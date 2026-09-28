import os
import threading
import time
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


# =========================================================
# SUPABASE
# =========================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = None

if SUPABASE_URL and SUPABASE_KEY:
    try:
        supabase = create_client(
            SUPABASE_URL,
            SUPABASE_KEY
        )
        print("SUPABASE BAGLANTISI OK")
    except Exception as e:
        print("SUPABASE BAGLANTI HATASI:", e)


# =========================================================
# BINANCE TESTNET
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

        binance = None

else:

    print(
        "BINANCE API bilgileri bulunamadi!"
    )


# =========================================================
# AUTOTRADER AYARLARI
# =========================================================

TIMEFRAME = Client.KLINE_INTERVAL_15MINUTE

BALANCE_PERCENT = 100.0

TAKE_PROFIT_PERCENT = 10.0

STOP_LOSS_PERCENT = 5.0

SCAN_INTERVAL = 30

KLINE_LIMIT = 100

MIN_SCORE = 55.0


# =========================================================
# LOCK
# =========================================================

buy_lock = threading.Lock()


# =========================================================
# POZISYON
# =========================================================

position_open = False

position_symbol = None

entry_price = 0.0

position_quantity = 0.0

last_scanned_candle = None


# =========================================================
# AKTIF USDT PARITELERI
# =========================================================

def get_usdt_symbols():

    info = binance.get_exchange_info()

    symbols = []

    for item in info["symbols"]:

        try:

            if (
                item["status"] == "TRADING"
                and item["quoteAsset"] == "USDT"
                and item.get(
                    "isSpotTradingAllowed",
                    False
                )
            ):

                symbols.append(
                    item["symbol"]
                )

        except Exception:

            continue

    print("================================")
    print(
        f"[SYMBOLS] {len(symbols)} aktif USDT paritesi bulundu."
    )
    print("================================")

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
        ) + ema

    return ema


# =========================================================
# RSI
# =========================================================

def calculate_rsi(values, period=14):

    if len(values) < period + 1:
        return None

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

def calculate_atr(
    candles,
    period=14
):

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
            abs(
                high
                - previous_close
            ),
            abs(
                low
                - previous_close
            )
        )

        true_ranges.append(tr)

        previous_close = float(
            candle[4]
        )

    return (
        sum(
            true_ranges[-period:]
        ) / period
    )


# =========================================================
# PARITE KURALLARI
# =========================================================

def get_symbol_rules(symbol):

    info = binance.get_symbol_info(
        symbol
    )

    if not info:

        raise RuntimeError(
            f"Symbol bilgisi bulunamadi: {symbol}"
        )

    step_size = 0.000001

    min_qty = 0.0

    min_notional = 0.0

    max_notional = 0.0

    for f in info["filters"]:

        filter_type = f[
            "filterType"
        ]

        # MARKET_LOT_SIZE varsa
        # market emirleri icin onu kullan.
        if filter_type == "MARKET_LOT_SIZE":

            step_size = float(
                f["stepSize"]
            )

            min_qty = float(
                f["minQty"]
            )

        elif (
            filter_type
            == "LOT_SIZE"
        ):

            if step_size == 0.000001:

                step_size = float(
                    f["stepSize"]
                )

                min_qty = float(
                    f["minQty"]
                )

        elif filter_type in (
            "MIN_NOTIONAL",
            "NOTIONAL"
        ):

            min_notional = max(
                min_notional,
                float(
                    f.get(
                        "minNotional",
                        0
                    )
                )
            )

            max_notional = max(
                max_notional,
                float(
                    f.get(
                        "maxNotional",
                        0
                    )
                )
            )

    return (
        step_size,
        min_qty,
        min_notional,
        max_notional
    )


# =========================================================
# PARITE ANALIZI
# =========================================================

def analyze_symbol(symbol):

    candles = binance.get_klines(
        symbol=symbol,
        interval=TIMEFRAME,
        limit=KLINE_LIMIT
    )

    if len(candles) < 60:
        return None

    # Son mum kapanmamis olabilir.
    # Sadece kapanmis mumlari kullan.
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
    # ONEMLI:
    # Bunlar HER DURUMDA once tanimlaniyor.
    # volume_ratio hatasi artik olusmayacak.
    # =====================================================

    momentum = 0.0

    volume_ratio = 0.0

    # Momentum

    if len(closes) >= 6:

        old_price = closes[-6]

        if old_price > 0:

            momentum = (
                (
                    closes[-1]
                    / old_price
                ) - 1
            ) * 100

    # Volume

    if len(volumes) >= 21:

        average_volume = (
            sum(
                volumes[-21:-1]
            ) / 20
        )

        if average_volume > 0:

            volume_ratio = (
                volumes[-1]
                / average_volume
            )

    # =====================================================
    # SKOR
    # =====================================================

    score = 0.0

    # 1 TREND - 25

    if ema20 > ema50:

        score += 15

        ema_gap = (
            (
                ema20
                - ema50
            )
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

        score -= 20

    # 2 PRICE / EMA20 - 15

    if current > ema20:

        distance = (
            (
                current
                - ema20
            )
            / ema20
        ) * 100

        score += min(
            15,
            max(
                0,
                distance * 5
            )
        )

    # 3 MOMENTUM - 20

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

    # 4 RSI - 15

    if 50 <= rsi <= 70:

        rsi_score = (
            15
            - abs(rsi - 60)
            * 0.5
        )

        score += max(
            0,
            rsi_score
        )

    elif 70 < rsi <= 75:

        score += 5

    elif rsi > 75:

        score -= 10

    # 5 VOLUME - 15

    if volume_ratio >= 1:

        score += min(
            15,
            volume_ratio * 7
        )

    # 6 BREAKOUT - 10

    if len(highs) >= 21:

        previous_high = max(
            highs[-21:-1]
        )

        if current > previous_high:

            score += 10

    # ATR

    atr_percent = (
        atr
        / current
    ) * 100

    if atr_percent < 0.15:

        score -= 5

    if atr_percent > 8:

        score -= 5

    return {

        "symbol": symbol,

        "score": round(
            score,
            2
        ),

        "price": current,

        "ema20": ema20,

        "ema50": ema50,

        "rsi": rsi,

        "atr_percent":
            atr_percent,

        "momentum":
            momentum,

        "volume_ratio":
            volume_ratio,

    }


# =========================================================
# EN GUCLU PARITE
# =========================================================

def find_best_symbol(
    symbols,
    usdt_balance
):

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

            # -------------------------------------------------
            # Onemli:
            # %100 USDT ile BUY yapilamayan pariteleri
            # en bastan ele.
            # -------------------------------------------------

            try:

                (
                    step_size,
                    min_qty,
                    min_notional,
                    max_notional
                ) = get_symbol_rules(
                    symbol
                )

            except Exception:

                continue

            if (
                min_notional > 0
                and usdt_balance
                < min_notional
            ):

                continue

            if (
                max_notional > 0
                and usdt_balance
                > max_notional
            ):

                # Bu pariteye %100 bakiye sigmiyor.
                # Baska parite ara.
                continue

            result = analyze_symbol(
                symbol
            )

            if result:

                results.append(
                    result
                )

            # Binance'i gereksiz
            # hizlandirmamak icin.
            time.sleep(0.08)

            if index % 25 == 0:

                print(
                    f"[SCAN] {index}/{total} tamamlandi..."
                )

        except Exception as e:

            print(
                f"[SCAN ERROR] {symbol}: {e}"
            )

    if not results:

        print(
            "[SCAN] Gecerli analiz bulunamadi."
        )

        return None

    # Skora gore buyukten kucuge.
    results.sort(
        key=lambda x:
            x["score"],
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

    print("TOP 10:")

    for item in results[:10]:

        print(
            f"{item['symbol']} | "
            f"Skor: {item['score']} | "
            f"RSI: {item['rsi']:.2f} | "
            f"Momentum: {item['momentum']:.2f}% | "
            f"Hacim: {item['volume_ratio']:.2f}"
        )

    print("================================")

    if best["score"] < MIN_SCORE:

        print(
            f"[NO TRADE] En yuksek skor "
            f"{best['score']}. "
            f"Minimum: {MIN_SCORE}"
        )

        return None

    return best


# =========================================================
# ACCOUNT
# =========================================================

def get_account():

    return binance.get_account()


# =========================================================
# USDT BALANCE
# =========================================================

def get_usdt_balance():

    account = get_account()

    for balance in account["balances"]:

        if balance["asset"] == "USDT":

            return float(
                balance["free"]
            )

    return 0.0


# =========================================================
# ASSET BALANCE
# =========================================================

def get_asset_balance(asset):

    account = get_account()

    for balance in account["balances"]:

        if balance["asset"] == asset:

            return float(
                balance["free"]
            )

    return 0.0


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
# MEVCUT POZISYONU BUL
# =========================================================

def find_existing_position(
    symbols
):

    global position_open
    global position_symbol
    global position_quantity
    global entry_price

    try:

        account = get_account()

        balances = {
            b["asset"]:
                float(b["free"])

            for b in account[
                "balances"
            ]

            if float(
                b["free"]
            ) > 0
        }

        tickers = (
            binance.get_all_tickers()
        )

        prices = {
            x["symbol"]:
                float(x["price"])

            for x in tickers
        }

        candidates = []

        for symbol in symbols:

            if not symbol.endswith(
                "USDT"
            ):

                continue

            base = symbol[:-4]

            qty = balances.get(
                base,
                0.0
            )

            price = prices.get(
                symbol,
                0.0
            )

            if qty <= 0:
                continue

            if price <= 0:
                continue

            try:

                (
                    step_size,
                    min_qty,
                    min_notional,
                    max_notional
                ) = get_symbol_rules(
                    symbol
                )

            except Exception:

                continue

            value = (
                qty
                * price
            )

            threshold = max(
                min_notional,
                1.0
            )

            if value >= threshold:

                candidates.append(
                    (
                        value,
                        symbol,
                        qty,
                        price
                    )
                )

        if not candidates:

            return False

        candidates.sort(
            reverse=True
        )

        (
            _,
            symbol,
            qty,
            current_price
        ) = candidates[0]

        recovered_entry = (
            current_price
        )

        try:

            trades = (
                binance.get_my_trades(
                    symbol=symbol,
                    limit=50
                )
            )

            buys = [
                t
                for t in trades
                if t.get(
                    "isBuyer"
                )
            ]

            if buys:

                last_buy = buys[-1]

                last_qty = float(
                    last_buy.get(
                        "qty",
                        0
                    )
                )

                last_price = float(
                    last_buy.get(
                        "price",
                        0
                    )
                )

                if (
                    last_qty > 0
                    and last_price > 0
                ):

                    recovered_entry = (
                        last_price
                    )

        except Exception as e:

            print(
                "[POSITION SYNC] "
                "Trade gecmisi okunamadi:",
                e
            )

        position_open = True

        position_symbol = symbol

        position_quantity = qty

        entry_price = recovered_entry

        print("================================")
        print(
            "🔒 MEVCUT POZISYON BULUNDU"
        )
        print(
            "PARITE:",
            symbol
        )
        print(
            "MIKTAR:",
            qty
        )
        print(
            "GIRIS FIYATI:",
            recovered_entry
        )
        print(
            "TEKRAR BUY YAPILMAYACAK"
        )
        print("================================")

        return True

    except Exception as e:

        print(
            "[POSITION SYNC HATASI]",
            e
        )

        return False


# =========================================================
# BUY
# =========================================================

def buy_symbol(symbol):

    global position_open
    global position_symbol
    global entry_price
    global position_quantity

    with buy_lock:

        if position_open:

            print(
                "[BUY ENGEL] Zaten acik pozisyon var:",
                position_symbol
            )

            return False

        try:

            usdt_balance = (
                get_usdt_balance()
            )

            if usdt_balance <= 0:

                print(
                    "[BUY ENGEL] "
                    "USDT bakiyesi yok."
                )

                return False

            (
                step_size,
                min_qty,
                min_notional,
                max_notional
            ) = get_symbol_rules(
                symbol
            )

            # =================================================
            # NOTIONAL KONTROLU
            # =================================================

            if (
                min_notional > 0
                and usdt_balance
                < min_notional
            ):

                print(
                    "[BUY ENGEL] "
                    "MIN NOTIONAL:",
                    min_notional
                )

                return False

            if (
                max_notional > 0
                and usdt_balance
                > max_notional
            ):

                print(
                    "[BUY ENGEL] "
                    "MAX NOTIONAL:",
                    max_notional
                )

                return False

            trade_amount = (
                usdt_balance
                * BALANCE_PERCENT
                / 100.0
            )

            print("================================")
            print(
                "🟢 BUY DENENIYOR"
            )
            print(
                "PARITE:",
                symbol
            )
            print(
                "SKORLU KAZANAN:",
                symbol
            )
            print(
                "USDT:",
                trade_amount
            )
            print(
                "BAKIYE KULLANIMI: %100"
            )
            print(
                "MIN NOTIONAL:",
                min_notional
            )
            print(
                "MAX NOTIONAL:",
                max_notional
            )
            print("================================")

            quote_amount = round(
                trade_amount,
                2
            )

            try:

                order = (
                    binance.create_order(
                        symbol=symbol,
                        side="BUY",
                        type="MARKET",
                        quoteOrderQty=quote_amount
                    )
                )

            except Exception as first_error:

                error_text = str(
                    first_error
                ).lower()

                # Tam bakiye fee/precision
                # yuzunden reddedilirse.
                if (
                    "insufficient"
                    in error_text
                    or "balance"
                    in error_text
                ):

                    fallback_amount = round(
                        trade_amount
                        * 0.995,
                        2
                    )

                    print(
                        "[BUY RETRY] "
                        "Tam bakiye reddedildi. "
                        "%99.5 deneniyor."
                    )

                    order = (
                        binance.create_order(
                            symbol=symbol,
                            side="BUY",
                            type="MARKET",
                            quoteOrderQty=
                                fallback_amount
                        )
                    )

                else:

                    raise

            executed_qty = float(
                order.get(
                    "executedQty",
                    0
                )
            )

            spent = float(
                order.get(
                    "cummulativeQuoteQty",
                    0
                )
            )

            status = order.get(
                "status",
                "UNKNOWN"
            )

            if executed_qty <= 0:

                print(
                    "[BUY] Emir gerceklesmedi."
                )

                return False

            if status not in (
                "FILLED",
                "PARTIALLY_FILLED"
            ):

                print(
                    "[BUY] Emir durumu:",
                    status
                )

                return False

            if spent <= 0:

                spent = quote_amount

            entry_price = (
                spent
                / executed_qty
            )

            position_quantity = (
                executed_qty
            )

            position_symbol = (
                symbol
            )

            position_open = True

            print("================================")
            print(
                "✅ TESTNET BUY GERCEKLESTI"
            )
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
                "HARCANAN USDT:",
                spent
            )
            print(
                "STATUS:",
                status
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
# SELL
# =========================================================

def sell_position(reason):

    global position_open
    global position_symbol
    global entry_price
    global position_quantity

    if (
        not position_open
        or not position_symbol
    ):

        return False

    symbol = position_symbol

    try:

        (
            step_size,
            min_qty,
            min_notional,
            max_notional
        ) = get_symbol_rules(
            symbol
        )

        quantity = round_quantity(
            position_quantity,
            step_size
        )

        if quantity <= 0:

            print(
                "[SELL] Miktar 0."
            )

            return False

        if quantity < min_qty:

            print(
                "[SELL] "
                "Minimum miktarin altinda."
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

        order = (
            binance.create_order(
                symbol=symbol,
                side="SELL",
                type="MARKET",
                quantity=quantity
            )
        )

        print("================================")
        print(
            "✅ TESTNET SELL GERCEKLESTI"
        )
        print(
            "PARITE:",
            symbol
        )
        print(
            "SEBEP:",
            reason
        )
        print(
            "STATUS:",
            order.get(
                "status"
            )
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
# POZISYON IZLE
# =========================================================

def monitor_position():

    if (
        not position_open
        or not position_symbol
        or entry_price <= 0
    ):

        return

    try:

        ticker = (
            binance.get_symbol_ticker(
                symbol=position_symbol
            )
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

        if (
            change
            >= TAKE_PROFIT_PERCENT
        ):

            sell_position(
                "TAKE PROFIT %10"
            )

        elif (
            change
            <= -STOP_LOSS_PERCENT
        ):

            sell_position(
                "STOP LOSS %5"
            )

    except Exception as e:

        print(
            "[POSITION ERROR]",
            e
        )


# =========================================================
# SON KAPANAN 15M MUM
# =========================================================

def get_latest_closed_candle_time():

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


# =========================================================
# AUTOTRADER
# =========================================================

def auto_trader():

    global last_scanned_candle

    print("================================")
    print(
        "🤖 AUTOTRADER BASLADI"
    )
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
        "STRATEJI: "
        "EMA + RSI + MOMENTUM + "
        "HACIM + BREAKOUT"
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

    # Restart sonrasi mevcut coin varsa
    # yeni BUY yapma.
    find_existing_position(
        symbols
    )

    while True:

        try:

            if binance is None:

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            # Pozisyon varsa tarama yapma.
            if position_open:

                monitor_position()

                time.sleep(10)

                continue

            candle_time = (
                get_latest_closed_candle_time()
            )

            if candle_time is None:

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            # Ayni mumu tekrar tarama.
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
                "[NEW CANDLE] "
                "Yeni 15 dakikalik mum kapandi."
            )

            print(
                f"[SCAN] "
                f"{len(symbols)} parite "
                f"taramasi basliyor..."
            )

            print("================================")

            # =================================================
            # USDT BAKIYESI
            # =================================================

            usdt_balance = (
                get_usdt_balance()
            )

            print(
                "[BALANCE] USDT:",
                usdt_balance
            )

            if usdt_balance <= 0:

                print(
                    "[NO TRADE] "
                    "USDT bakiyesi yok."
                )

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            # =================================================
            # 487+ PARITE TARA
            # =================================================

            best = find_best_symbol(
                symbols,
                usdt_balance
            )

            if best is None:

                print(
                    "[NO TRADE] "
                    "Bu mumda uygun parite yok."
                )

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            # =================================================
            # SON POZISYON KONTROLU
            # =================================================

            if find_existing_position(
                symbols
            ):

                print(
                    "[NO TRADE] "
                    "Hesapta zaten pozisyon var."
                )

                time.sleep(
                    SCAN_INTERVAL
                )

                continue

            # =================================================
            # TRADE ADAYI
            # =================================================

            print("================================")

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

            print("================================")

            # =================================================
            # SADECE EN YUKSEK SKORLU PARITE
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

            time.sleep(60)


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

    if supabase:

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
                        user.last_name,
                },
                on_conflict=
                    "telegram_id"
            ).execute()

        except Exception as e:

            print(
                "SUPABASE START HATASI:",
                e
            )

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

    try:

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
            stop_signals=None,
            drop_pending_updates=True
        )

    except Exception as e:

        print(
            "TELEGRAM BOT HATASI:",
            e
        )


threading.Thread(
    target=run_bot,
    daemon=True
).start()
