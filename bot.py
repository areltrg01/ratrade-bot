import os
import threading
import time
import math
from decimal import Decimal, ROUND_DOWN

from flask import Flask, render_template
from supabase import create_client
from binance.client import Client

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)


# =========================================================
# SUPABASE
# =========================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)


# =========================================================
# BINANCE
# =========================================================

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")

binance = None


if BINANCE_API_KEY and BINANCE_API_SECRET:

    try:

        binance = Client(
            BINANCE_API_KEY,
            BINANCE_API_SECRET,
            testnet=True
        )

        account = binance.get_account()

        print("================================")
        print("BINANCE TESTNET BAGLANTISI OK")
        print("Binance hesabina erisim basarili")
        print("================================")

        for balance in account["balances"]:

            if float(balance["free"]) > 0:

                print(
                    balance["asset"],
                    balance["free"]
                )

    except Exception as e:

        print(
            "BINANCE BAGLANTI HATASI:",
            e
        )

else:

    print(
        "BINANCE API bilgileri bulunamadi!"
    )


# =========================================================
# AUTOTRADER AYARLARI
# =========================================================

TIMEFRAME = Client.KLINE_INTERVAL_15MINUTE

BALANCE_PERCENT = 100

TAKE_PROFIT_PERCENT = 10.0
STOP_LOSS_PERCENT = 5.0

SCAN_INTERVAL = 60

KLINE_LIMIT = 100

MIN_SCORE = 55.0


# =========================================================
# POZISYON
# =========================================================

position_open = False

position_symbol = None

entry_price = 0.0

position_quantity = 0.0

last_scanned_candle = None


# =========================================================
# AKTIF USDT PARITELERİ
# =========================================================

def get_usdt_symbols():

    info = binance.get_exchange_info()

    symbols = []

    for item in info["symbols"]:

        try:

            if (
                item["status"] == "TRADING"
                and item["quoteAsset"] == "USDT"
                and item.get("isSpotTradingAllowed", False)
            ):

                symbols.append(
                    item["symbol"]
                )

        except Exception:

            continue

    print(
        "================================"
    )

    print(
        f"[SYMBOLS] {len(symbols)} aktif USDT paritesi bulundu."
    )

    print(
        "================================"
    )

    return symbols


# =========================================================
# EMA
# =========================================================

def calculate_ema(values, period):

    if len(values) < period:

        return None

    multiplier = 2 / (period + 1)

    ema = sum(
        values[:period]
    ) / period

    for price in values[period:]:

        ema = (
            (price - ema)
            * multiplier
            + ema
        )

    return ema


# =========================================================
# RSI
# =========================================================

def calculate_rsi(values, period=14):

    if len(values) < period + 1:

        return None

    gains = []
    losses = []

    for i in range(1, len(values)):

        change = (
            values[i]
            - values[i - 1]
        )

        if change > 0:

            gains.append(change)
            losses.append(0)

        else:

            gains.append(0)
            losses.append(abs(change))

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

    rs = avg_gain / avg_loss

    return 100 - (
        100 / (1 + rs)
    )


# =========================================================
# ATR
# =========================================================

def calculate_atr(candles, period=14):

    if len(candles) < period + 1:

        return None

    true_ranges = []

    previous_close = float(
        candles[0][4]
    )

    for candle in candles[1:]:

        high = float(candle[2])
        low = float(candle[3])

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        true_ranges.append(tr)

        previous_close = float(
            candle[4]
        )

    return (
        sum(true_ranges[-period:])
        / period
    )


# =========================================================
# PARİTE ANALİZİ
# =========================================================

def analyze_symbol(symbol):

    candles = binance.get_klines(
        symbol=symbol,
        interval=TIMEFRAME,
        limit=KLINE_LIMIT
    )

    if len(candles) < 60:

        return None

    # Son mum henüz kapanmamış olabilir.
    # Sadece kapanmış mumları kullanıyoruz.
    closed = candles[:-1]

    closes = [
        float(c[4])
        for c in closed
    ]

    highs = [
        float(c[2])
        for c in closed
    ]

    volumes = [
        float(c[5])
        for c in closed
    ]

    if len(closes) < 55:

        return None

    current = closes[-1]

    ema20 = calculate_ema(
        closes,
        20
    )

    ema50 = calculate_ema(
        closes,
        50
    )

    rsi = calculate_rsi(
        closes,
        14
    )

    atr = calculate_atr(
        closed,
        14
    )

    if (
        ema20 is None
        or ema50 is None
        or rsi is None
        or atr is None
    ):

        return None

    # =====================================================
    # SCORE
    # =====================================================

    score = 0.0

    # -----------------------------------------------------
    # 1. TREND - 25 PUAN
    # -----------------------------------------------------

    if ema20 > ema50:

        score += 15

        ema_gap = (
            (ema20 - ema50)
            / ema50
        ) * 100

        score += min(
            10,
            max(
                0,
                ema_gap * 5
            )
        )

    else:

        # Güçlü yükseliş trendi yok
        score -= 20


    # -----------------------------------------------------
    # 2. FİYAT / EMA20 - 15 PUAN
    # -----------------------------------------------------

    if current > ema20:

        distance = (
            (current - ema20)
            / ema20
        ) * 100

        score += min(
            15,
            max(
                0,
                distance * 5
            )
        )


    # -----------------------------------------------------
    # 3. MOMENTUM - 20 PUAN
    # -----------------------------------------------------

    if len(closes) >= 6:

        momentum = (
            (
                closes[-1]
                / closes[-6]
            ) - 1
        ) * 100

        if momentum > 0:

            score += min(
                20,
                momentum * 8
            )

        else:

            score += max(
                -10,
                momentum * 3
            )


    # -----------------------------------------------------
    # 4. RSI - 15 PUAN
    # -----------------------------------------------------

    if 50 <= rsi <= 70:

        # 60 civarı ideal bölge
        rsi_score = (
            15
            - abs(rsi - 60) * 0.5
        )

        score += max(
            0,
            rsi_score
        )

    elif 70 < rsi <= 75:

        score += 5

    elif rsi > 75:

        score -= 10


    # -----------------------------------------------------
    # 5. HACİM - 15 PUAN
    # -----------------------------------------------------

    if len(volumes) >= 21:

        average_volume = (
            sum(volumes[-21:-1])
            / 20
        )

        if average_volume > 0:

            volume_ratio = (
                volumes[-1]
                / average_volume
            )

            if volume_ratio >= 1:

                score += min(
                    15,
                    volume_ratio * 7
                )


    # -----------------------------------------------------
    # 6. BREAKOUT - 10 PUAN
    # -----------------------------------------------------

    if len(highs) >= 21:

        previous_high = max(
            highs[-21:-1]
        )

        if current > previous_high:

            score += 10


    # -----------------------------------------------------
    # ATR VOLATILITY
    # -----------------------------------------------------

    atr_percent = (
        atr / current
    ) * 100


    # Aşırı düşük volatiliteyi cezalandır
    if atr_percent < 0.15:

        score -= 5


    # Aşırı volatiliteyi de biraz cezalandır
    if atr_percent > 8:

        score -= 5


    return {
        "symbol": symbol,
        "score": round(score, 2),
        "price": current,
        "ema20": ema20,
        "ema50": ema50,
        "rsi": rsi,
        "atr_percent": atr_percent,
        "momentum": momentum if len(closes) >= 6 else 0,
        "volume_ratio": volume_ratio if len(volumes) >= 21 else 0,
    }


# =========================================================
# EN GÜÇLÜ PARİTEYİ BUL
# =========================================================

def find_best_symbol(symbols):

    results = []

    total = len(symbols)

    print("================================")
    print(
        f"[SCAN] {total} parite taranacak."
    )
    print(
        "[SCAN] 15 dakikalik kapanmis mumlar kullaniliyor."
    )
    print("================================")


    for index, symbol in enumerate(
        symbols,
        start=1
    ):

        try:

            result = analyze_symbol(
                symbol
            )

            if result:

                results.append(
                    result
                )

            # API'yi gereksiz zorlamamak için
            # küçük bekleme.
            time.sleep(0.08)


            if index % 25 == 0:

                print(
                    f"[SCAN] {index}/{total} tamamlandi..."
                )


        except Exception as e:

            print(
                f"[SCAN ERROR] "
                f"{symbol}: {e}"
            )

            continue


    if not results:

        print(
            "[SCAN] Gecerli analiz bulunamadi."
        )

        return None


    results.sort(
        key=lambda x: x["score"],
        reverse=True
    )


    best = results[0]


    print("================================")
    print("🏆 EN GUCLU PARITE")
    print("================================")

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
        round(best["rsi"], 2)
    )

    print(
        "MOMENTUM:",
        round(best["momentum"], 2),
        "%"
    )

    print(
        "HACIM ORANI:",
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

    print("================================")


    # En yüksek birkaç sonucu da göster
    print("TOP 10:")

    for item in results[:10]:

        print(
            f"{item['symbol']} | "
            f"Skor: {item['score']} | "
            f"RSI: {item['rsi']:.2f} | "
            f"Momentum: {item['momentum']:.2f}%"
        )

    print("================================")


    if best["score"] < MIN_SCORE:

        print(
            f"[NO TRADE] En yuksek skor "
            f"{best['score']}. Minimum: {MIN_SCORE}"
        )

        return None


    return best


# =========================================================
# USDT BAKİYESİ
# =========================================================

def get_usdt_balance():

    account = binance.get_account()

    for balance in account["balances"]:

        if balance["asset"] == "USDT":

            return float(
                balance["free"]
            )

    return 0.0


# =========================================================
# ASSET BAKİYESİ
# =========================================================

def get_asset_balance(asset):

    account = binance.get_account()

    for balance in account["balances"]:

        if balance["asset"] == asset:

            return float(
                balance["free"]
            )

    return 0.0


# =========================================================
# SYMBOL KURALLARI
# =========================================================

def get_symbol_rules(symbol):

    info = binance.get_symbol_info(
        symbol
    )

    step_size = 0.000001
    min_qty = 0.0
    min_notional = 0.0

    for f in info["filters"]:

        if f["filterType"] == "LOT_SIZE":

            step_size = float(
                f["stepSize"]
            )

            min_qty = float(
                f["minQty"]
            )

        elif f["filterType"] == "MIN_NOTIONAL":

            min_notional = float(
                f["minNotional"]
            )

        elif f["filterType"] == "NOTIONAL":

            min_notional = float(
                f["minNotional"]
            )

    return (
        step_size,
        min_qty,
        min_notional
    )


# =========================================================
# QUANTITY YUVARLAMA
# =========================================================

def round_quantity(
    quantity,
    step_size
):

    step = Decimal(
        str(step_size)
    )

    value = Decimal(
        str(quantity)
    )

    rounded = (
        value / step
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * step

    return float(
        rounded
    )


# =========================================================
# ALIŞ
# =========================================================

def buy_symbol(symbol):

    global position_open
    global position_symbol
    global entry_price
    global position_quantity

    usdt_balance = get_usdt_balance()

    if usdt_balance <= 0:

        print(
            "[BUY] USDT bakiyesi yok."
        )

        return False


    trade_amount = (
        usdt_balance
        * BALANCE_PERCENT
        / 100
    )


    print("================================")
    print("🟢 BUY")
    print(
        "PARITE:",
        symbol
    )
    print(
        "USDT:",
        trade_amount
    )
    print(
        "BAKIYE KULLANIMI: %100"
    )
    print("================================")


    try:

        order = binance.create_order(
            symbol=symbol,
            side="BUY",
            type="MARKET",
            quoteOrderQty=round(
                trade_amount,
                2
            )
        )


        executed_qty = float(
            order.get(
                "executedQty",
                0
            )
        )


        spent = float(
            order.get(
                "cummulativeQuoteQty",
                trade_amount
            )
        )


        if executed_qty <= 0:

            print(
                "[BUY] Emir dolmadi."
            )

            return False


        entry_price = (
            spent
            / executed_qty
        )

        position_quantity = (
            executed_qty
        )

        position_symbol = symbol

        position_open = True


        print("================================")
        print("✅ TESTNET BUY GERCEKLESTI")
        print(
            "PARITE:",
            symbol
        )
        print(
            "MIKTAR:",
            executed_qty
        )
        print(
            "GIRIS FIYATI:",
            entry_price
        )
        print(
            "HARcanan USDT:",
            spent
        )
        print("================================")


        return True


    except Exception as e:

        print(
            "[BUY HATASI]",
            symbol,
            e
        )

        return False


# =========================================================
# SATIŞ
# =========================================================

def sell_position(reason):

    global position_open
    global position_symbol
    global entry_price
    global position_quantity

    if not position_open:

        return False


    symbol = position_symbol

    base_asset = symbol.replace(
        "USDT",
        ""
    )


    try:

        balance = get_asset_balance(
            base_asset
        )


        step_size, min_qty, min_notional = (
            get_symbol_rules(
                symbol
            )
        )


        quantity = round_quantity(
            balance,
            step_size
        )


        if quantity <= 0:

            print(
                "[SELL] Satilacak miktar yok."
            )

            return False


        if quantity < min_qty:

            print(
                "[SELL] Minimum miktarin altinda."
            )

            return False


        print("================================")
        print("🔴 SELL")
        print(
            "PARITE:",
            symbol
        )
        print(
            "SEBEP:",
            reason
        )
        print(
            "MIKTAR:",
            quantity
        )
        print("================================")


        order = binance.create_order(
            symbol=symbol,
            side="SELL",
            type="MARKET",
            quantity=quantity
        )


        print("================================")
        print("✅ TESTNET SELL GERCEKLESTI")
        print(
            "PARITE:",
            symbol
        )
        print(
            "SEBEP:",
            reason
        )
        print("================================")


        position_open = False
        position_symbol = None
        entry_price = 0.0
        position_quantity = 0.0


        return True


    except Exception as e:

        print(
            "[SELL HATASI]",
            symbol,
            e
        )

        return False


# =========================================================
# AÇIK POZİSYON KONTROLÜ
# =========================================================

def monitor_position():

    if not position_open:

        return


    try:

        ticker = binance.get_symbol_ticker(
            symbol=position_symbol
        )

        current_price = float(
            ticker["price"]
        )


        change = (
            (
                current_price
                - entry_price
            )
            / entry_price
        ) * 100


        print(
            f"[POSITION] "
            f"{position_symbol} | "
            f"Giris: {entry_price:.8f} | "
            f"Fiyat: {current_price:.8f} | "
            f"P/L: {change:.2f}%"
        )


        if change >= TAKE_PROFIT_PERCENT:

            sell_position(
                "TAKE PROFIT %10"
            )


        elif change <= -STOP_LOSS_PERCENT:

            sell_position(
                "STOP LOSS %5"
            )


    except Exception as e:

        print(
            "[POSITION ERROR]",
            e
        )


# =========================================================
# YENİ 15 DAKİKALIK MUMU BUL
# =========================================================

def get_latest_closed_candle_time():

    candles = binance.get_klines(
        symbol="BTCUSDT",
        interval=TIMEFRAME,
        limit=3
    )

    if len(candles) < 2:

        return None

    # Son eleman açık mum olabilir.
    # Ondan önceki mum son kapanmış mumdur.
    return candles[-2][0]


# =========================================================
# AUTOTRADER
# =========================================================

def auto_trader():

    global last_scanned_candle

    print("================================")
    print("🤖 AUTOTRADER BASLADI")
    print("================================")
    print(
        "MUM: 15 DAKIKA"
    )
    print(
        "PARITELER: TUM AKTIF USDT SPOT"
    )
    print(
        "BAKIYE: %100"
    )
    print(
        "TAKE PROFIT: %10"
    )
    print(
        "STOP LOSS: %5"
    )
    print(
        "STRATEJI: EMA + RSI + MOMENTUM + HACIM + BREAKOUT"
    )
    print(
        "MIN SCORE:",
        MIN_SCORE
    )
    print(
        "BINANCE: TESTNET"
    )
    print("================================")


    symbols = get_usdt_symbols()


    while True:

        try:

            if binance is None:

                time.sleep(
                    SCAN_INTERVAL
                )

                continue


            # =================================================
            # AÇIK POZİSYON
            # =================================================

            if position_open:

                monitor_position()

                time.sleep(
                    10
                )

                continue


            # =================================================
            # SON KAPANMIŞ 15M MUM
            # =================================================

            candle_time = (
                get_latest_closed_candle_time()
            )


            if candle_time is None:

                time.sleep(
                    SCAN_INTERVAL
                )

                continue


            # Aynı mumda tekrar tarama yapma
            if (
                last_scanned_candle
                == candle_time
            ):

                time.sleep(
                    SCAN_INTERVAL
                )

                continue


            last_scanned_candle = (
                candle_time
            )


            print("================================")
            print(
                "[NEW CANDLE] Yeni 15 dakikalik mum kapandi."
            )
            print(
                "[SCAN] 487+ parite taramasi basliyor..."
            )
            print("================================")


            # =================================================
            # TÜM PARİTELERİ TARA
            # =================================================

            best = find_best_symbol(
                symbols
            )


            if best is None:

                print(
                    "[NO TRADE] Bu mumda uygun parite yok."
                )

                time.sleep(
                    SCAN_INTERVAL
                )

                continue


            # =================================================
            # EN GÜÇLÜ PARİTE
            # =================================================

            print("================================")
            print("🏆 TRADE ADAYI")
            print(
                best["symbol"]
            )
            print(
                "SKOR:",
                best["score"]
            )
            print("================================")


            # =================================================
            # %100 USDT İLE AL
            # =================================================

            buy_symbol(
                best["symbol"]
            )


            time.sleep(
                SCAN_INTERVAL
            )


        except Exception as e:

            print(
                "AUTOTRADER GENEL HATA:",
                e
            )

            time.sleep(
                60
            )


# =========================================================
# AUTOTRADER THREAD
# =========================================================

if binance:

    threading.Thread(
        target=auto_trader,
        daemon=True
    ).start()


# =========================================================
# MINI APP
# =========================================================

@app.route("/")
def home():

    return render_template(
        "index.html"
    )


# =========================================================
# TELEGRAM START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    supabase.table(
        "users"
    ).upsert(
        {
            "telegram_id": user.id,
            "first_name": user.first_name,
            "last_name": user.last_name,
        },
        on_conflict="telegram_id",
    ).execute()


    await update.message.reply_text(
        "🤖 RaTrade Bot'a hoş geldin!\n\n"
        "Mini App'e giriş yapabilirsin. 🚀"
    )


# =========================================================
# TELEGRAM BOT
# =========================================================

def run_bot():

    token = os.getenv(
        "TELEGRAM_BOT_TOKEN"
    )

    if not token:

        print(
            "TELEGRAM_BOT_TOKEN bulunamadı!"
        )

        return


    bot = (
        ApplicationBuilder()
        .token(token)
        .build()
    )


    bot.add_handler(
        CommandHandler(
            "start",
            start
        )
    )


    bot.run_polling(
        stop_signals=None
    )


threading.Thread(
    target=run_bot,
    daemon=True
).start()
