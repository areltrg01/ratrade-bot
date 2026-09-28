import os
import time
import threading
import math
from decimal import Decimal, ROUND_DOWN

from flask import Flask
from binance.client import Client
from binance.exceptions import BinanceAPIException

from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes


# =========================================================
# AYARLAR
# =========================================================

BINANCE_API_KEY = os.getenv("BINANCE_API_KEY")
BINANCE_API_SECRET = os.getenv("BINANCE_API_SECRET")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")

# SADECE TESTNET
BINANCE_TESTNET = True

TIMEFRAME = Client.KLINE_INTERVAL_15MINUTE

# Kullanılacak bakiye
BALANCE_PERCENT = 100.0

# Kar / zarar
TAKE_PROFIT_PERCENT = 10.0
STOP_LOSS_PERCENT = 5.0

# Her yeni 15 dakikalık mumda tarama
SCAN_INTERVAL = 10

# Her pariteden alınacak mum sayısı
KLINE_LIMIT = 100

# Minimum skor
MIN_SCORE = 55.0

# =========================================================
# FLASK
# =========================================================

app = Flask(__name__)


@app.route("/")
def home():
    return "RA TRADE BOT AKTIF"


@app.route("/health")
def health():
    return "OK"


# =========================================================
# BINANCE
# =========================================================

binance = None

if BINANCE_API_KEY and BINANCE_API_SECRET:
    try:
        binance = Client(
            BINANCE_API_KEY,
            BINANCE_API_SECRET,
            testnet=BINANCE_TESTNET
        )

        print("BINANCE TESTNET BAGLANTISI OK")

        try:
            account = binance.get_account()

            usdt_balance = 0.0

            for asset in account.get("balances", []):
                if asset["asset"] == "USDT":
                    usdt_balance = float(asset["free"])
                    break

            print("Binance hesabina erisim basarili")
            print(f"USDT {usdt_balance}")

        except Exception as e:
            print("[BINANCE ACCOUNT ERROR]", e)

    except Exception as e:
        print("[BINANCE BAGLANTI HATASI]", e)
        binance = None
else:
    print("[BINANCE] API bilgileri eksik")


# =========================================================
# AUTOTRADER DEĞİŞKENLERİ
# =========================================================

position = None

symbols = []

last_closed_candle = None

trader_running = False


# =========================================================
# YARDIMCI
# =========================================================

def safe_float(value, default=0.0):
    try:
        return float(value)
    except Exception:
        return default


def calculate_ema(values, period):
    if not values:
        return 0.0

    if len(values) < period:
        return sum(values) / len(values)

    multiplier = 2 / (period + 1)

    ema = sum(values[:period]) / period

    for price in values[period:]:
        ema = (price - ema) * multiplier + ema

    return ema


def calculate_rsi(values, period=14):
    if len(values) <= period:
        return 50.0

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]

        if change > 0:
            gains.append(change)
            losses.append(0)
        else:
            gains.append(0)
            losses.append(abs(change))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for i in range(period, len(gains)):
        avg_gain = ((avg_gain * (period - 1)) + gains[i]) / period
        avg_loss = ((avg_loss * (period - 1)) + losses[i]) / period

    if avg_loss == 0:
        return 100.0

    rs = avg_gain / avg_loss

    return 100 - (100 / (1 + rs))


def calculate_atr(highs, lows, closes, period=14):
    if len(closes) <= period:
        return 0.0

    true_ranges = []

    for i in range(1, len(closes)):
        high = highs[i]
        low = lows[i]
        previous_close = closes[i - 1]

        tr = max(
            high - low,
            abs(high - previous_close),
            abs(low - previous_close)
        )

        true_ranges.append(tr)

    if len(true_ranges) < period:
        return 0.0

    return sum(true_ranges[-period:]) / period


# =========================================================
# USDT PARİTELERİ
# =========================================================

def get_usdt_symbols():

    global symbols

    try:
        exchange_info = binance.get_exchange_info()

        result = []

        for item in exchange_info.get("symbols", []):

            if item.get("status") != "TRADING":
                continue

            if item.get("quoteAsset") != "USDT":
                continue

            if not item.get("isSpotTradingAllowed", True):
                continue

            symbol = item.get("symbol")

            if symbol:
                result.append(symbol)

        result = sorted(set(result))

        symbols = result

        print(f"[SYMBOLS] {len(result)} aktif USDT paritesi bulundu.")

        return result

    except Exception as e:
        print("[SYMBOL ERROR]", e)
        return []


# =========================================================
# PARİTE ANALİZİ
# =========================================================

def analyze_symbol(symbol):

    try:

        candles = binance.get_klines(
            symbol=symbol,
            interval=TIMEFRAME,
            limit=KLINE_LIMIT
        )

        if not candles or len(candles) < 30:
            return None

        # Son mum açık olabilir.
        # Sadece kapanmış mumları kullanıyoruz.
        closed_candles = candles[:-1]

        if len(closed_candles) < 30:
            return None

        opens = [safe_float(x[1]) for x in closed_candles]
        highs = [safe_float(x[2]) for x in closed_candles]
        lows = [safe_float(x[3]) for x in closed_candles]
        closes = [safe_float(x[4]) for x in closed_candles]
        volumes = [safe_float(x[5]) for x in closed_candles]

        if not closes:
            return None

        price = closes[-1]

        # -------------------------------------------------
        # EMA
        # -------------------------------------------------

        ema20 = calculate_ema(closes, 20)
        ema50 = calculate_ema(closes, 50)

        # -------------------------------------------------
        # RSI
        # -------------------------------------------------

        rsi = calculate_rsi(closes, 14)

        # -------------------------------------------------
        # ATR
        # -------------------------------------------------

        atr = calculate_atr(
            highs,
            lows,
            closes,
            14
        )

        # -------------------------------------------------
        # MOMENTUM
        # -------------------------------------------------

        momentum = 0.0

        if len(closes) >= 7:

            old_price = closes[-7]

            if old_price > 0:
                momentum = (
                    (price - old_price)
                    / old_price
                ) * 100

        # -------------------------------------------------
        # VOLUME RATIO
        # ÖNEMLİ:
        # Burada artık her durumda değer atanıyor.
        # volume_ratio hatası olmayacak.
        # -------------------------------------------------

        volume_ratio = 1.0

        if len(volumes) >= 21:

            previous_volumes = volumes[-21:-1]

            if previous_volumes:

                average_volume = (
                    sum(previous_volumes)
                    / len(previous_volumes)
                )

                if average_volume > 0:

                    volume_ratio = (
                        volumes[-1]
                        / average_volume
                    )

        # -------------------------------------------------
        # BREAKOUT
        # -------------------------------------------------

        breakout = False

        if len(highs) >= 21:

            previous_highs = highs[-21:-1]

            if previous_highs:

                highest_previous = max(previous_highs)

                if price > highest_previous:
                    breakout = True

        # -------------------------------------------------
        # SCORE
        # -------------------------------------------------

        score = 0.0

        # Trend
        if ema20 > ema50:

            score += 25

            # EMA farkı ne kadar büyükse ekstra puan
            trend_percent = (
                (ema20 - ema50)
                / ema50
            ) * 100 if ema50 > 0 else 0

            if trend_percent >= 2:
                score += 5

        # Fiyat EMA20 üstünde
        if price > ema20:

            score += 15

            price_distance = (
                (price - ema20)
                / ema20
            ) * 100 if ema20 > 0 else 0

            if price_distance >= 1:
                score += 3

        # Momentum
        if momentum > 0:

            momentum_score = min(
                max(momentum * 2, 0),
                20
            )

            score += momentum_score

        # RSI
        if 50 <= rsi <= 70:

            score += 15

        elif 45 <= rsi < 50:

            score += 8

        elif 70 < rsi <= 75:

            score += 8

        # Volume
        if volume_ratio >= 1.5:

            score += 15

        elif volume_ratio >= 1.2:

            score += 12

        elif volume_ratio >= 1.0:

            score += 8

        elif volume_ratio >= 0.8:

            score += 4

        # Breakout
        if breakout:

            score += 10

        # ATR aşırı yüksekse biraz puan düşür
        if price > 0 and atr > 0:

            atr_percent = (
                atr / price
            ) * 100

            if atr_percent > 8:

                score -= 10

            elif atr_percent > 5:

                score -= 5

        # 0-100 arasında tut
        score = max(
            0.0,
            min(score, 100.0)
        )

        return {
            "symbol": symbol,
            "score": round(score, 2),
            "price": price,
            "ema20": ema20,
            "ema50": ema50,
            "rsi": rsi,
            "momentum": momentum,
            "volume_ratio": volume_ratio,
            "atr": atr,
            "breakout": breakout
        }

    except Exception as e:

        print(
            f"[SCAN ERROR] {symbol}: {e}"
        )

        return None


# =========================================================
# 487 PARİTE TARAMASI
# =========================================================

def find_best_symbol():

    if not symbols:

        print("[SCAN] Parite listesi bos.")

        return None

    print()
    print("=" * 60)
    print("[SCAN] 487+ parite taramasi basliyor...")
    print("=" * 60)

    results = []

    total = len(symbols)

    for index, symbol in enumerate(symbols, start=1):

        result = analyze_symbol(symbol)

        if result is not None:

            results.append(result)

        if index % 25 == 0 or index == total:

            print(
                f"[SCAN] {index}/{total} tamamlandi..."
            )

        # Binance rate limit'i zorlamamak için
        time.sleep(0.05)

    if not results:

        print("[SCAN] Gecerli sonuc bulunamadi.")

        return None

    # Skora göre büyükten küçüğe sırala
    results.sort(
        key=lambda x: x["score"],
        reverse=True
    )

    best = results[0]

    print()
    print("🏆 EN GUCLU PARITE")
    print(f"PARITE: {best['symbol']}")
    print(f"SKOR: {best['score']}")
    print(f"FIYAT: {best['price']}")
    print(f"EMA20: {best['ema20']}")
    print(f"EMA50: {best['ema50']}")
    print(f"RSI: {best['rsi']:.2f}")
    print(f"MOMENTUM: {best['momentum']:.2f}%")
    print(f"VOLUME RATIO: {best['volume_ratio']:.2f}")
    print(f"ATR: {best['atr']}")
    print(f"BREAKOUT: {best['breakout']}")

    print()
    print("🏆 TOP 10")

    for i, item in enumerate(
        results[:10],
        start=1
    ):

        print(
            f"{i}. {item['symbol']} "
            f"| SKOR {item['score']} "
            f"| RSI {item['rsi']:.2f} "
            f"| MOM {item['momentum']:.2f}% "
            f"| VOL {item['volume_ratio']:.2f}"
        )

    print("=" * 60)

    if best["score"] < MIN_SCORE:

        print(
            f"[TRADE] En yuksek skor {best['score']}."
        )

        print(
            f"[TRADE] Minimum skor {MIN_SCORE}. "
            f"Bu mumda ALIM YOK."
        )

        return None

    return best


# =========================================================
# SEMBOL FİLTRELERİ
# =========================================================

def get_symbol_filters(symbol):

    try:

        info = binance.get_symbol_info(symbol)

        if not info:
            return None

        filters = {}

        for f in info.get("filters", []):

            filter_type = f.get("filterType")

            filters[filter_type] = f

        return filters

    except Exception as e:

        print(
            f"[FILTER ERROR] {symbol}: {e}"
        )

        return None


def round_quantity(symbol, quantity):

    filters = get_symbol_filters(symbol)

    if not filters:
        return quantity

    # Market emirlerinde varsa MARKET_LOT_SIZE kullan
    lot_filter = (
        filters.get("MARKET_LOT_SIZE")
        or filters.get("LOT_SIZE")
    )

    if not lot_filter:
        return quantity

    step_size = safe_float(
        lot_filter.get("stepSize"),
        0
    )

    min_qty = safe_float(
        lot_filter.get("minQty"),
        0
    )

    if step_size <= 0:
        return quantity

    quantity_decimal = Decimal(
        str(quantity)
    )

    step_decimal = Decimal(
        str(step_size)
    )

    rounded = (
        quantity_decimal
        / step_decimal
    ).to_integral_value(
        rounding=ROUND_DOWN
    ) * step_decimal

    rounded_quantity = float(
        rounded
    )

    if rounded_quantity < min_qty:
        return 0.0

    return rounded_quantity


# =========================================================
# USDT BAKİYE
# =========================================================

def get_free_usdt():

    try:

        account = binance.get_account()

        for asset in account.get("balances", []):

            if asset["asset"] == "USDT":

                return safe_float(
                    asset["free"]
                )

    except Exception as e:

        print(
            "[BALANCE ERROR]",
            e
        )

    return 0.0


# =========================================================
# ALIM
# =========================================================

def buy_symbol(symbol):

    global position

    try:

        usdt_balance = get_free_usdt()

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

        print()
        print("🟢 BUY")
        print(f"PARITE: {symbol}")
        print(f"USDT: {trade_amount}")

        # İlk deneme %100
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

        except BinanceAPIException as e:

            # Bazı paritelerde exact balance
            # NOTIONAL / fee nedeniyle reddedilebilir.
            print(
                f"[BUY %100 HATASI] {symbol}: {e}"
            )

            print(
                "[BUY] %99.5 bakiye ile tekrar deneniyor..."
            )

            fallback_amount = (
                usdt_balance
                * 0.995
            )

            order = binance.create_order(
                symbol=symbol,
                side="BUY",
                type="MARKET",
                quoteOrderQty=round(
                    fallback_amount,
                    2
                )
            )

        executed_qty = safe_float(
            order.get("executedQty")
        )

        quote_qty = safe_float(
            order.get("cummulativeQuoteQty")
        )

        if executed_qty <= 0:

            print(
                "[BUY] Emir gerceklestirilmedi."
            )

            return False

        if quote_qty > 0:

            entry_price = (
                quote_qty
                / executed_qty
            )

        else:

            ticker = binance.get_symbol_ticker(
                symbol=symbol
            )

            entry_price = safe_float(
                ticker["price"]
            )

        position = {
            "symbol": symbol,
            "quantity": executed_qty,
            "entry_price": entry_price,
            "order_id": order.get("orderId"),
            "time": time.time()
        }

        print()
        print("✅ ALIM GERCEKLESTI")
        print(f"PARITE: {symbol}")
        print(f"MIKTAR: {executed_qty}")
        print(f"GIRIS: {entry_price}")
        print(
            f"TP: {entry_price * 1.10}"
        )
        print(
            f"SL: {entry_price * 0.95}"
        )

        return True

    except Exception as e:

        print(
            f"[BUY HATASI] {symbol}: {e}"
        )

        return False


# =========================================================
# SATIŞ
# =========================================================

def sell_position(reason):

    global position

    if not position:
        return False

    symbol = position["symbol"]

    quantity = position["quantity"]

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
        print("🔴 SELL")
        print(f"PARITE: {symbol}")
        print(f"MIKTAR: {quantity}")
        print(f"NEDEN: {reason}")

        order = binance.create_order(
            symbol=symbol,
            side="SELL",
            type="MARKET",
            quantity=quantity
        )

        executed_qty = safe_float(
            order.get("executedQty")
        )

        quote_qty = safe_float(
            order.get("cummulativeQuoteQty")
        )

        if executed_qty > 0 and quote_qty > 0:

            sell_price = (
                quote_qty
                / executed_qty
            )

        else:

            ticker = binance.get_symbol_ticker(
                symbol=symbol
            )

            sell_price = safe_float(
                ticker["price"]
            )

        entry = position["entry_price"]

        pnl_percent = (
            (sell_price - entry)
            / entry
        ) * 100

        print()
        print("✅ SATIS GERCEKLESTI")
        print(f"PARITE: {symbol}")
        print(f"GIRIS: {entry}")
        print(f"CIKIS: {sell_price}")
        print(
            f"P/L: {pnl_percent:.2f}%"
        )

        position = None

        return True

    except Exception as e:

        print(
            f"[SELL HATASI] {symbol}: {e}"
        )

        return False


# =========================================================
# POZİSYON TAKİBİ
# =========================================================

def monitor_position():

    global position

    if not position:
        return

    symbol = position["symbol"]

    entry = position["entry_price"]

    try:

        ticker = binance.get_symbol_ticker(
            symbol=symbol
        )

        current_price = safe_float(
            ticker["price"]
        )

        if current_price <= 0:
            return

        pnl_percent = (
            (current_price - entry)
            / entry
        ) * 100

        print(
            f"[POSITION] {symbol} "
            f"| GIRIS {entry:.8f} "
            f"| SIMDI {current_price:.8f} "
            f"| P/L {pnl_percent:.2f}%"
        )

        # TAKE PROFIT
        if pnl_percent >= TAKE_PROFIT_PERCENT:

            print(
                "🎯 TAKE PROFIT"
            )

            sell_position(
                "TAKE PROFIT +10%"
            )

            return

        # STOP LOSS
        if pnl_percent <= -STOP_LOSS_PERCENT:

            print(
                "🛑 STOP LOSS"
            )

            sell_position(
                "STOP LOSS -5%"
            )

            return

    except Exception as e:

        print(
            "[POSITION ERROR]",
            e
        )


# =========================================================
# SON KAPANMIŞ MUM
# =========================================================

def get_latest_closed_candle_time():

    try:

        candles = binance.get_klines(
            symbol="BTCUSDT",
            interval=TIMEFRAME,
            limit=3
        )

        if len(candles) < 2:
            return None

        # Son mum halen açık olabilir.
        # Ondan önceki kapanmış mum.
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

    global last_closed_candle
    global trader_running

    if not binance:

        print(
            "[AUTOTRADER] Binance baglantisi yok."
        )

        return

    if trader_running:

        print(
            "[AUTOTRADER] Zaten calisiyor."
        )

        return

    trader_running = True

    print()
    print("=" * 60)
    print("AUTOTRADER BASLADI")
    print("MUM: 15 DAKIKA")
    print("PARITE: AKTIF USDT SPOT")
    print("BAKIYE: %100")
    print("TAKE PROFIT: +10%")
    print("STOP LOSS: -5%")
    print("POZISYON: AYNI ANDA 1")
    print("=" * 60)

    # İlk açılışta pariteleri al
    get_usdt_symbols()

    while True:

        try:

            # Pozisyon açıksa
            # yeni tarama yapma
            if position:

                monitor_position()

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            # Son kapanmış mum
            closed_candle = (
                get_latest_closed_candle_time()
            )

            if closed_candle is None:

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            # Aynı mumda ikinci kez tarama yapma
            if (
                last_closed_candle
                == closed_candle
            ):

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            # Yeni mum
            last_closed_candle = (
                closed_candle
            )

            print()
            print(
                "[NEW CANDLE] Yeni 15 dakikalik "
                "mum kapandi."
            )

            # Pariteleri güncelle
            get_usdt_symbols()

            # 487+ pariteyi tara
            best = find_best_symbol()

            if not best:

                print(
                    "[TRADE] Bu mumda islem yok."
                )

                continue

            print()
            print("🏆 TRADE ADAYI")
            print(
                f"PARITE: {best['symbol']}"
            )
            print(
                f"SKOR: {best['score']}"
            )

            # Alım
            success = buy_symbol(
                best["symbol"]
            )

            if not success:

                print(
                    "[TRADE] ALIM YAPILAMADI."
                )

        except Exception as e:

            print(
                "[AUTOTRADER ERROR]",
                e
            )

            time.sleep(10)


# =========================================================
# TELEGRAM
# =========================================================

async def start_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    user = update.effective_user

    if user:

        print(
            f"[TELEGRAM] /start "
            f"{user.id} "
            f"{user.first_name}"
        )

    await update.message.reply_text(
        "🚀 RA Trade aktif."
    )


def run_telegram():

    if not TELEGRAM_BOT_TOKEN:

        print(
            "[TELEGRAM] Token bulunamadi."
        )

        return

    try:

        application = (
            Application.builder()
            .token(TELEGRAM_BOT_TOKEN)
            .build()
        )

        application.add_handler(
            CommandHandler(
                "start",
                start_command
            )
        )

        print(
            "[TELEGRAM] Bot baslatiliyor..."
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

if __name__ == "__main__":

    # AutoTrader ayrı thread
    trader_thread = threading.Thread(
        target=auto_trader,
        daemon=True
    )

    trader_thread.start()

    # Telegram
    run_telegram()
