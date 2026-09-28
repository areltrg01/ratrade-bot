import os
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_DOWN

from flask import Flask, render_template
from supabase import create_client
from binance.client import Client
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)


# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)


@app.route("/")
def home():
    return render_template("index.html")


@app.route("/health")
def health():
    return "OK"


# =========================================================
# ENV
# =========================================================

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")


# =========================================================
# SUPABASE
# =========================================================

supabase = None

if SUPABASE_URL and SUPABASE_KEY:

    try:
        supabase = create_client(
            SUPABASE_URL,
            SUPABASE_KEY
        )

        print("SUPABASE BAGLANTISI OK")

    except Exception as e:

        print(
            "[SUPABASE HATASI]",
            e
        )


# =========================================================
# BINANCE TESTNET
# =========================================================

binance = None

if BINANCE_API_KEY and BINANCE_API_SECRET:

    try:

        binance = Client(
            BINANCE_API_KEY,
            BINANCE_API_SECRET,
            testnet=True
        )

        account = binance.get_account()

        usdt = 0.0

        for b in account.get("balances", []):

            if b["asset"] == "USDT":

                usdt = float(
                    b["free"]
                )

                break

        print("================================")
        print("BINANCE TESTNET BAGLANTISI OK")
        print("Binance hesabina erisim basarili")
        print("USDT:", usdt)
        print("================================")

    except Exception as e:

        print(
            "[BINANCE BAGLANTI HATASI]",
            e
        )

        binance = None

else:

    print(
        "[BINANCE] API bilgileri eksik"
    )


# =========================================================
# AYARLAR
# =========================================================

TIMEFRAME = Client.KLINE_INTERVAL_15MINUTE

KLINE_LIMIT = 100

MIN_SCORE = 55.0

TAKE_PROFIT_PERCENT = 10.0

STOP_LOSS_PERCENT = 5.0

BALANCE_PERCENT = 100.0

SCAN_INTERVAL = 10

# Aynı anda kaç API isteği
# yapılacağını kontrol eder.
SCAN_WORKERS = 10


# =========================================================
# DURUM
# =========================================================

symbols = []

last_scanned_candle = None

position = None

trader_started = False

telegram_started = False


# =========================================================
# YARDIMCI
# =========================================================

def f(value, default=0.0):

    try:
        return float(value)

    except Exception:
        return default


# =========================================================
# EMA
# =========================================================

def ema(values, period):

    if not values:
        return 0.0

    if len(values) < period:

        return (
            sum(values)
            / len(values)
        )

    multiplier = 2 / (
        period + 1
    )

    result = (
        sum(values[:period])
        / period
    )

    for price in values[period:]:

        result = (
            (price - result)
            * multiplier
        ) + result

    return result


# =========================================================
# RSI
# =========================================================

def rsi(values, period=14):

    if len(values) <= period:

        return 50.0

    gains = []
    losses = []

    for i in range(
        1,
        len(values)
    ):

        change = (
            values[i]
            - values[i - 1]
        )

        if change > 0:

            gains.append(change)
            losses.append(0)

        else:

            gains.append(0)
            losses.append(
                abs(change)
            )

    avg_gain = (
        sum(gains[:period])
        / period
    )

    avg_loss = (
        sum(losses[:period])
        / period
    )

    for i in range(
        period,
        len(gains)
    ):

        avg_gain = (
            (
                avg_gain
                * (period - 1)
            )
            + gains[i]
        ) / period

        avg_loss = (
            (
                avg_loss
                * (period - 1)
            )
            + losses[i]
        ) / period

    if avg_loss == 0:

        return 100.0

    rs = (
        avg_gain
        / avg_loss
    )

    return 100 - (
        100 / (1 + rs)
    )


# =========================================================
# ATR
# =========================================================

def atr(
    highs,
    lows,
    closes,
    period=14
):

    if len(closes) <= period:

        return 0.0

    trs = []

    for i in range(
        1,
        len(closes)
    ):

        high = highs[i]

        low = lows[i]

        previous = closes[i - 1]

        tr = max(
            high - low,
            abs(
                high
                - previous
            ),
            abs(
                low
                - previous
            )
        )

        trs.append(tr)

    if len(trs) < period:

        return 0.0

    return (
        sum(trs[-period:])
        / period
    )


# =========================================================
# PARİTELER
# =========================================================

def get_usdt_symbols():

    global symbols

    try:

        info = (
            binance.get_exchange_info()
        )

        result = []

        for item in info.get(
            "symbols",
            []
        ):

            if item.get(
                "status"
            ) != "TRADING":

                continue

            if item.get(
                "quoteAsset"
            ) != "USDT":

                continue

            if not item.get(
                "isSpotTradingAllowed",
                True
            ):

                continue

            symbol = item.get(
                "symbol"
            )

            if symbol:

                result.append(
                    symbol
                )

        symbols = sorted(
            set(result)
        )

        print(
            f"[SYMBOLS] "
            f"{len(symbols)} aktif USDT "
            f"paritesi bulundu."
        )

        return symbols

    except Exception as e:

        print(
            "[SYMBOL ERROR]",
            e
        )

        return []


# =========================================================
# TEK PARİTE ANALİZİ
# =========================================================

def analyze_symbol(symbol):

    # -----------------------------------------------------
    # BU DEĞERLER HER DURUMDA TANIMLI.
    # Önceki volume_ratio hatasının ana koruması.
    # -----------------------------------------------------

    volume_ratio = 1.0

    momentum = 0.0

    breakout = False

    try:

        candles = (
            binance.get_klines(
                symbol=symbol,
                interval=TIMEFRAME,
                limit=KLINE_LIMIT
            )
        )

        if not candles:

            return None

        if len(candles) < 30:

            return None

        # Açık olan son mumu kullanma.
        closed = candles[:-1]

        if len(closed) < 30:

            return None

        highs = [
            f(x[2])
            for x in closed
        ]

        lows = [
            f(x[3])
            for x in closed
        ]

        closes = [
            f(x[4])
            for x in closed
        ]

        volumes = [
            f(x[5])
            for x in closed
        ]

        if not closes:

            return None

        price = closes[-1]

        if price <= 0:

            return None

        # -------------------------------------------------
        # EMA
        # -------------------------------------------------

        ema20 = ema(
            closes,
            20
        )

        ema50 = ema(
            closes,
            50
        )

        # -------------------------------------------------
        # RSI
        # -------------------------------------------------

        rsi_value = rsi(
            closes,
            14
        )

        # -------------------------------------------------
        # ATR
        # -------------------------------------------------

        atr_value = atr(
            highs,
            lows,
            closes,
            14
        )

        # -------------------------------------------------
        # MOMENTUM
        # -------------------------------------------------

        if len(closes) >= 7:

            old_price = closes[-7]

            if old_price > 0:

                momentum = (
                    (
                        price
                        / old_price
                    ) - 1
                ) * 100

        # -------------------------------------------------
        # VOLUME
        # -------------------------------------------------

        if len(volumes) >= 21:

            previous_volumes = (
                volumes[-21:-1]
            )

            if previous_volumes:

                average_volume = (
                    sum(
                        previous_volumes
                    )
                    / len(
                        previous_volumes
                    )
                )

                if average_volume > 0:

                    volume_ratio = (
                        volumes[-1]
                        / average_volume
                    )

        # -------------------------------------------------
        # BREAKOUT
        # -------------------------------------------------

        if len(highs) >= 21:

            previous_highs = (
                highs[-21:-1]
            )

            if previous_highs:

                highest = max(
                    previous_highs
                )

                if price > highest:

                    breakout = True

        # -------------------------------------------------
        # SKOR
        # -------------------------------------------------

        score = 0.0

        # TREND
        if ema20 > ema50:

            score += 25

            if ema50 > 0:

                trend_percent = (
                    (
                        ema20
                        - ema50
                    )
                    / ema50
                ) * 100

                score += min(
                    5,
                    max(
                        0,
                        trend_percent
                    )
                )

        else:

            score -= 20

        # PRICE > EMA20
        if price > ema20:

            score += 15

        # MOMENTUM
        if momentum > 0:

            score += min(
                20,
                momentum * 2
            )

        # RSI
        if 50 <= rsi_value <= 70:

            score += 15

        elif 45 <= rsi_value < 50:

            score += 8

        elif 70 < rsi_value <= 75:

            score += 8

        # VOLUME
        if volume_ratio >= 1.5:

            score += 15

        elif volume_ratio >= 1.2:

            score += 12

        elif volume_ratio >= 1.0:

            score += 8

        elif volume_ratio >= 0.8:

            score += 4

        # BREAKOUT
        if breakout:

            score += 10

        # ATR
        atr_percent = 0.0

        if price > 0:

            atr_percent = (
                atr_value
                / price
            ) * 100

        if atr_percent > 8:

            score -= 10

        elif atr_percent > 5:

            score -= 5

        score = max(
            0,
            min(
                score,
                100
            )
        )

        return {

            "symbol":
                symbol,

            "score":
                round(
                    score,
                    2
                ),

            "price":
                price,

            "ema20":
                ema20,

            "ema50":
                ema50,

            "rsi":
                rsi_value,

            "momentum":
                momentum,

            "volume_ratio":
                volume_ratio,

            "atr_percent":
                atr_percent,

            "breakout":
                breakout
        }

    except Exception as e:

        # BİR PARİTE HATA VERİRSE
        # DİĞERLERİ DEVAM EDER.
        print(
            f"[SCAN ERROR] "
            f"{symbol}: {e}"
        )

        return None


# =========================================================
# TÜM PARİTELERİ TARA
# =========================================================

def find_best_symbol():

    if not symbols:

        return None

    total = len(symbols)

    print()
    print(
        "=========================================="
    )

    print(
        f"[SCAN] {total} parite taranacak."
    )

    print(
        "=========================================="
    )

    results = []

    completed = 0

    # -----------------------------------------------------
    # 10 kontrollü worker.
    # 487 coin tek tek beklemek yerine
    # kontrollü paralel taranır.
    # -----------------------------------------------------

    with ThreadPoolExecutor(
        max_workers=SCAN_WORKERS
    ) as executor:

        futures = {
            executor.submit(
                analyze_symbol,
                symbol
            ): symbol

            for symbol in symbols
        }

        for future in as_completed(
            futures
        ):

            completed += 1

            try:

                result = future.result()

                if result is not None:

                    results.append(
                        result
                    )

            except Exception as e:

                symbol = futures[
                    future
                ]

                print(
                    f"[SCAN ERROR] "
                    f"{symbol}: {e}"
                )

            if (
                completed % 25 == 0
                or completed == total
            ):

                print(
                    f"[SCAN] "
                    f"{completed}/{total} "
                    f"tamamlandi..."
                )

    print()
    print(
        f"[SCAN] "
        f"TARAMA TAMAMLANDI "
        f"{len(results)}/{total} "
        f"gecerli sonuc."
    )

    if not results:

        print(
            "[SCAN] Hicbir gecerli sonuc yok."
        )

        return None

    # EN YÜKSEK SKOR
    results.sort(
        key=lambda x:
            x["score"],
        reverse=True
    )

    best = results[0]

    print()
    print(
        "=========================================="
    )

    print(
        "🏆 EN GUCLU PARITE"
    )

    print(
        "PARITE:",
        best["symbol"]
    )

    print(
        "SKOR:",
        best["score"]
    )

    print(
        "FIYAT:",
        best["price"]
    )

    print(
        "EMA20:",
        best["ema20"]
    )

    print(
        "EMA50:",
        best["ema50"]
    )

    print(
        "RSI:",
        round(
            best["rsi"],
            2
        )
    )

    print(
        "MOMENTUM:",
        round(
            best["momentum"],
            2
        ),
        "%"
    )

    print(
        "VOLUME RATIO:",
        round(
            best["volume_ratio"],
            2
        )
    )

    print(
        "ATR:",
        round(
            best["atr_percent"],
            2
        ),
        "%"
    )

    print(
        "BREAKOUT:",
        best["breakout"]
    )

    print(
        "=========================================="
    )

    # TOP 10
    print()
    print(
        "🏆 TOP 10"
    )

    for i, item in enumerate(
        results[:10],
        start=1
    ):

        print(
            f"{i}. "
            f"{item['symbol']} | "
            f"SKOR {item['score']} | "
            f"RSI {item['rsi']:.2f} | "
            f"MOM {item['momentum']:.2f}% | "
            f"VOL {item['volume_ratio']:.2f}"
        )

    print(
        "=========================================="
    )

    # Minimum skor
    if best["score"] < MIN_SCORE:

        print(
            f"[NO TRADE] "
            f"En yuksek skor: "
            f"{best['score']}"
        )

        print(
            f"[NO TRADE] "
            f"Minimum skor: "
            f"{MIN_SCORE}"
        )

        return None

    return best


# =========================================================
# USDT BAKİYE
# =========================================================

def get_usdt_balance():

    try:

        account = (
            binance.get_account()
        )

        for item in account.get(
            "balances",
            []
        ):

            if item["asset"] == "USDT":

                return f(
                    item["free"]
                )

    except Exception as e:

        print(
            "[BALANCE ERROR]",
            e
        )

    return 0.0


# =========================================================
# FİLTRELER
# =========================================================

def get_symbol_filters(symbol):

    try:

        info = (
            binance.get_symbol_info(
                symbol
            )
        )

        if not info:

            return {}

        result = {}

        for item in info.get(
            "filters",
            []
        ):

            result[
                item["filterType"]
            ] = item

        return result

    except Exception as e:

        print(
            "[FILTER ERROR]",
            symbol,
            e
        )

        return {}


# =========================================================
# MİKTAR YUVARLAMA
# =========================================================

def round_quantity(
    symbol,
    quantity
):

    filters = (
        get_symbol_filters(
            symbol
        )
    )

    lot = (
        filters.get(
            "MARKET_LOT_SIZE"
        )
        or
        filters.get(
            "LOT_SIZE"
        )
    )

    if not lot:

        return quantity

    step = f(
        lot.get(
            "stepSize"
        ),
        0
    )

    minimum = f(
        lot.get(
            "minQty"
        ),
        0
    )

    if step <= 0:

        return quantity

    q = Decimal(
        str(quantity)
    )

    s = Decimal(
        str(step)
    )

    rounded = (
        q / s
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * s

    result = float(
        rounded
    )

    if result < minimum:

        return 0.0

    return result


# =========================================================
# BUY
# =========================================================

def buy_symbol(symbol):

    global position

    try:

        balance = (
            get_usdt_balance()
        )

        if balance <= 0:

            print(
                "[BUY] USDT yok."
            )

            return False

        filters = (
            get_symbol_filters(
                symbol
            )
        )

        min_notional = 0.0

        notional_filter = (
            filters.get(
                "NOTIONAL"
            )
            or
            filters.get(
                "MIN_NOTIONAL"
            )
        )

        if notional_filter:

            min_notional = f(
                notional_filter.get(
                    "minNotional"
                )
            )

        # %100 bakiye
        amount = (
            balance
            * BALANCE_PERCENT
            / 100
        )

        print()
        print(
            "================================"
        )

        print(
            "🟢 BUY DENENIYOR"
        )

        print(
            "PARITE:",
            symbol
        )

        print(
            "USDT:",
            amount
        )

        print(
            "MIN NOTIONAL:",
            min_notional
        )

        print(
            "================================"
        )

        # Önce %100 dene.
        try:

            order = (
                binance.create_order(
                    symbol=symbol,
                    side="BUY",
                    type="MARKET",
                    quoteOrderQty=round(
                        amount,
                        2
                    )
                )
            )

        except Exception as first_error:

            print(
                "[BUY %100 HATASI]",
                first_error
            )

            # Binance bazı durumlarda
            # komisyon/yuvarlama nedeniyle
            # tam bakiyeyi kabul etmeyebilir.
            # Güvenli geri dönüş.
            fallback = (
                balance
                * 0.995
            )

            print(
                "[BUY RETRY] "
                "%99.5 bakiye deneniyor."
            )

            order = (
                binance.create_order(
                    symbol=symbol,
                    side="BUY",
                    type="MARKET",
                    quoteOrderQty=round(
                        fallback,
                        2
                    )
                )
            )

        executed = f(
            order.get(
                "executedQty"
            )
        )

        spent = f(
            order.get(
                "cummulativeQuoteQty"
            )
        )

        if executed <= 0:

            print(
                "[BUY] Emir gerceklesmedi."
            )

            return False

        if spent > 0:

            entry = (
                spent
                / executed
            )

        else:

            ticker = (
                binance.get_symbol_ticker(
                    symbol=symbol
                )
            )

            entry = f(
                ticker["price"]
            )

        position = {

            "symbol":
                symbol,

            "quantity":
                executed,

            "entry_price":
                entry,

            "order_id":
                order.get(
                    "orderId"
                )
        }

        print()
        print(
            "================================"
        )

        print(
            "✅ BUY BASARILI"
        )

        print(
            "PARITE:",
            symbol
        )

        print(
            "MIKTAR:",
            executed
        )

        print(
            "GIRIS:",
            entry
        )

        print(
            "TAKE PROFIT:",
            f"{entry * 1.10:.8f}"
        )

        print(
            "STOP LOSS:",
            f"{entry * 0.95:.8f}"
        )

        print(
            "================================"
        )

        return True

    except Exception as e:

        print(
            "[BUY HATASI]",
            symbol,
            e
        )

        return False


# =========================================================
# SELL
# =========================================================

def sell_position(reason):

    global position

    if not position:

        return False

    symbol = (
        position["symbol"]
    )

    quantity = (
        position["quantity"]
    )

    try:

        quantity = round_quantity(
            symbol,
            quantity
        )

        if quantity <= 0:

            print(
                "[SELL] Gecerli miktar yok."
            )

            return False

        print()
        print(
            "🔴 SELL"
        )

        print(
            "PARITE:",
            symbol
        )

        print(
            "MIKTAR:",
            quantity
        )

        print(
            "SEBEP:",
            reason
        )

        order = (
            binance.create_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=quantity
            )
        )

        print(
            "✅ SELL BASARILI"
        )

        print(
            "STATUS:",
            order.get(
                "status"
            )
        )

        position = None

        return True

    except Exception as e:

        print(
            "[SELL HATASI]",
            symbol,
            e
        )

        return False


# =========================================================
# POZİSYON TAKİBİ
# =========================================================

def monitor_position():

    if not position:

        return

    symbol = (
        position["symbol"]
    )

    entry = (
        position["entry_price"]
    )

    try:

        ticker = (
            binance.get_symbol_ticker(
                symbol=symbol
            )
        )

        price = f(
            ticker["price"]
        )

        if price <= 0:

            return

        pnl = (
            (
                price
                - entry
            )
            / entry
        ) * 100

        print(
            f"[POSITION] "
            f"{symbol} | "
            f"Giris {entry:.8f} | "
            f"Fiyat {price:.8f} | "
            f"P/L {pnl:.2f}%"
        )

        if pnl >= TAKE_PROFIT_PERCENT:

            sell_position(
                "TAKE PROFIT +10%"
            )

        elif pnl <= -STOP_LOSS_PERCENT:

            sell_position(
                "STOP LOSS -5%"
            )

    except Exception as e:

        print(
            "[POSITION ERROR]",
            e
        )


# =========================================================
# KAPANMIŞ 15 DAKİKALIK MUM
# =========================================================

def get_latest_closed_candle():

    try:

        candles = (
            binance.get_klines(
                symbol="BTCUSDT",
                interval=TIMEFRAME,
                limit=3
            )
        )

        if len(candles) < 2:

            return None

        return candles[-2][0]

    except Exception as e:

        print(
            "[CANDLE ERROR]",
            e
        )

        return None


# =========================================================
# AUTOTRADER
# =========================================================

def auto_trader():

    global last_scanned_candle
    global trader_started

    if trader_started:

        return

    trader_started = True

    print()
    print(
        "=========================================="
    )

    print(
        "🤖 AUTOTRADER BASLADI"
    )

    print(
        "MUM: 15 DAKIKA"
    )

    print(
        "PARITE: AKTIF USDT"
    )

    print(
        "BAKIYE: %100"
    )

    print(
        "TAKE PROFIT: +10%"
    )

    print(
        "STOP LOSS: -5%"
    )

    print(
        "POZISYON: 1"
    )

    print(
        "=========================================="
    )

    # İlk parite listesini al.
    get_usdt_symbols()

    while True:

        try:

            if not binance:

                time.sleep(
                    30
                )

                continue

            # Pozisyon varsa
            # sadece takip.
            if position:

                monitor_position()

                time.sleep(
                    10
                )

                continue

            # Son kapanmış mum
            candle = (
                get_latest_closed_candle()
            )

            if candle is None:

                time.sleep(
                    10
                )

                continue

            # Aynı mumu ikinci kez tarama.
            if (
                candle
                == last_scanned_candle
            ):

                time.sleep(
                    10
                )

                continue

            last_scanned_candle = (
                candle
            )

            print()
            print(
                "=========================================="
            )

            print(
                "[NEW CANDLE] "
                "Yeni 15 dakikalik mum kapandi."
            )

            print(
                "=========================================="
            )

            # Parite listesini yenile.
            get_usdt_symbols()

            # 487+ pariteyi tara.
            best = (
                find_best_symbol()
            )

            if best is None:

                print(
                    "[NO TRADE] "
                    "Bu mumda uygun aday yok."
                )

                continue

            print()
            print(
                "=========================================="
            )

            print(
                "🏆 TRADE ADAYI"
            )

            print(
                "PARITE:",
                best["symbol"]
            )

            print(
                "SKOR:",
                best["score"]
            )

            print(
                "=========================================="
            )

            # SADECE EN YÜKSEK SKORLU
            # TEK PARİTE.
            buy_symbol(
                best["symbol"]
            )

        except Exception as e:

            print(
                "[AUTOTRADER ERROR]",
                e
            )

            time.sleep(
                10
            )


# =========================================================
# TELEGRAM
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = (
        update.effective_user
    )

    if supabase and user:

        try:

            supabase.table(
                "users"
            ).upsert(
                {
                    "telegram_id":
                        user.id,

                    "first_name":
                        user.first_name,

                    "last_name":
                        user.last_name
                },
                on_conflict=
                    "telegram_id"
            ).execute()

        except Exception as e:

            print(
                "[SUPABASE START ERROR]",
                e
            )

    await update.message.reply_text(
        "🤖 RaTrade Bot aktif.\n\n"
        "🚀 Mini App'e giriş yapabilirsin."
    )


def run_telegram():

    global telegram_started

    if telegram_started:

        return

    telegram_started = True

    if not TELEGRAM_BOT_TOKEN:

        print(
            "[TELEGRAM] TOKEN YOK"
        )

        return

    try:

        application = (
            ApplicationBuilder()
            .token(
                TELEGRAM_BOT_TOKEN
            )
            .build()
        )

        application.add_handler(
            CommandHandler(
                "start",
                start
            )
        )

        print(
            "[TELEGRAM] BOT BASLADI"
        )

        application.run_polling(
            drop_pending_updates=True
        )

    except Exception as e:

        print(
            "[TELEGRAM ERROR]",
            e
        )


# =========================================================
# BAŞLAT
# =========================================================

if binance:

    threading.Thread(
        target=auto_trader,
        daemon=True
    ).start()


if TELEGRAM_BOT_TOKEN:

    threading.Thread(
        target=run_telegram,
        daemon=True
    ).start()
