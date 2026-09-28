import os
import threading
import time
from decimal import Decimal, ROUND_DOWN

from flask import Flask, render_template
from supabase import create_client
from binance.client import Client

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


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

else:

    print(
        "BINANCE API bilgileri bulunamadi!"
    )


# =========================================================
# AUTOTRADER AYARLARI
# =========================================================

TIMEFRAME = Client.KLINE_INTERVAL_15MINUTE

FAST_PERIOD = 20
SLOW_PERIOD = 50

BALANCE_PERCENT = 100

TAKE_PROFIT_PERCENT = 2.0
STOP_LOSS_PERCENT = 1.0

CHECK_SECONDS = 60


# =========================================================
# POZISYON
# =========================================================

position_open = False

position_symbol = None
entry_price = 0.0
position_quantity = 0.0


# =========================================================
# USDT PARITELERINI AL
# =========================================================

def get_usdt_symbols():

    info = binance.get_exchange_info()

    symbols = []

    for item in info["symbols"]:

        if (
            item["status"] == "TRADING"
            and item["quoteAsset"] == "USDT"
            and item["isSpotTradingAllowed"]
        ):

            symbols.append(
                item["symbol"]
            )

    print(
        f"[SYMBOLS] {len(symbols)} aktif USDT paritesi bulundu."
    )

    return symbols


# =========================================================
# SYMBOL FILTRELERI
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
# MIKTAR YUVARLAMA
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
# USDT BAKIYESI
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
# COIN BAKIYESI
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
# FİYAT
# =========================================================

def get_price(symbol):

    ticker = binance.get_symbol_ticker(
        symbol=symbol
    )

    return float(
        ticker["price"]
    )


# =========================================================
# MUM VERISI
# =========================================================

def get_closes(symbol):

    candles = binance.get_klines(
        symbol=symbol,
        interval=TIMEFRAME,
        limit=SLOW_PERIOD + 3
    )

    closes = []

    for candle in candles:

        closes.append(
            float(candle[4])
        )

    return closes


# =========================================================
# ORTALAMA
# =========================================================

def average(values):

    if not values:

        return 0.0

    return (
        sum(values)
        / len(values)
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

    print(
        f"[BUY] {symbol} | "
        f"Kullanilacak USDT: {trade_amount:.2f}"
    )

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
                "[BUY] Emir gerceklestirilemedi."
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
        print("TESTNET BUY GERCEKLESTI")
        print(
            "PARITE:",
            symbol
        )
        print(
            "MIKTAR:",
            executed_qty
        )
        print(
            "GIRIS:",
            entry_price
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
            get_symbol_rules(symbol)
        )

        quantity = round_quantity(
            balance,
            step_size
        )

        if quantity < min_qty:

            print(
                "[SELL] Minimum miktarin altinda."
            )

            return False

        print(
            f"[SELL] {symbol} | "
            f"Sebep: {reason}"
        )

        order = binance.create_order(
            symbol=symbol,
            side="SELL",
            type="MARKET",
            quantity=quantity
        )

        print("================================")
        print("TESTNET SELL GERCEKLESTI")
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
# AUTOTRADER
# =========================================================

def auto_trader():

    global position_open

    print("================================")
    print("AUTOTRADER BASLADI")
    print("MUM: 15 DAKIKA")
    print("PARITELER: TUM USDT SPOT")
    print("BAKIYE: %100")
    print("TAKE PROFIT: %2")
    print("STOP LOSS: %1")
    print("BINANCE: TESTNET")
    print("================================")

    symbols = get_usdt_symbols()

    last_candle_check = 0

    while True:

        try:

            if not binance:

                time.sleep(
                    CHECK_SECONDS
                )

                continue


            # =================================================
            # ACIK POZISYON
            # =================================================

            if position_open:

                current_price = get_price(
                    position_symbol
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
                    f"Degisim: {change:.2f}%"
                )

                if change >= TAKE_PROFIT_PERCENT:

                    sell_position(
                        "TAKE PROFIT"
                    )

                elif change <= -STOP_LOSS_PERCENT:

                    sell_position(
                        "STOP LOSS"
                    )

                time.sleep(
                    CHECK_SECONDS
                )

                continue


            # =================================================
            # YENI MUM KONTROLU
            # =================================================

            current_time = time.time()

            if (
                current_time
                - last_candle_check
                < 60
            ):

                time.sleep(
                    CHECK_SECONDS
                )

                continue

            last_candle_check = current_time


            # =================================================
            # PARITELERI TARA
            # =================================================

            print(
                "================================"
            )

            print(
                "[SCAN] USDT pariteleri taraniyor..."
            )

            for symbol in symbols:

                try:

                    closes = get_closes(
                        symbol
                    )

                    if len(closes) < SLOW_PERIOD:

                        continue


                    # Son kapanmış mumu kullan
                    closed_closes = closes[:-1]

                    fast_average = average(
                        closed_closes[
                            -FAST_PERIOD:
                        ]
                    )

                    slow_average = average(
                        closed_closes[
                            -SLOW_PERIOD:
                        ]
                    )

                    current_price = get_price(
                        symbol
                    )

                    print(
                        f"[SCAN] {symbol} | "
                        f"Fiyat: {current_price:.8f} | "
                        f"SMA20: {fast_average:.8f} | "
                        f"SMA50: {slow_average:.8f}"
                    )


                    # =================================================
                    # ALIŞ SINYALI
                    # =================================================

                    if fast_average > slow_average:

                        print(
                            "================================"
                        )

                        print(
                            f"[BUY SIGNAL] {symbol}"
                        )

                        print(
                            "SMA20 > SMA50"
                        )

                        print(
                            "USDT bakiyesinin %100'u kullanilacak."
                        )

                        print(
                            "================================"
                        )

                        success = buy_symbol(
                            symbol
                        )

                        if success:

                            break


                except Exception as symbol_error:

                    print(
                        f"[SYMBOL HATASI] "
                        f"{symbol}: "
                        f"{symbol_error}"
                    )

                    continue


            time.sleep(
                CHECK_SECONDS
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
# TELEGRAM
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
# BOT
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
