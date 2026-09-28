import os
import threading

from flask import Flask, render_template
from supabase import create_client
from binance.client import Client

from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes


app = Flask(__name__)


# =========================
# SUPABASE
# =========================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


# =========================
# BINANCE TESTNET
# =========================

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
        print("BINANCE BAGLANTI HATASI:", e)

else:
    print("BINANCE API bilgileri bulunamadi!")


# =========================
# MINI APP
# =========================

@app.route("/")
def home():
    return render_template("index.html")


# =========================
# TELEGRAM
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    supabase.table("users").upsert(
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


# =========================
# BOT
# =========================

def run_bot():

    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token:
        print("TELEGRAM_BOT_TOKEN bulunamadı!")
        return

    bot = ApplicationBuilder().token(token).build()

    bot.add_handler(
        CommandHandler("start", start)
    )

    bot.run_polling(
        stop_signals=None
    )


threading.Thread(
    target=run_bot,
    daemon=True
).start()
