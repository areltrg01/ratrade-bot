import os
import threading
from flask import Flask
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

app = Flask(__name__)

@app.route("/")
def home():
    return "AutoTrade Bot aktif!"

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 AutoTrade Bot'a hoş geldin!\n\n"
        "Sistem hazırlanıyor..."
    )

def run_bot():
    token = os.getenv("TELEGRAM_BOT_TOKEN")

    if not token:
        print("TELEGRAM_BOT_TOKEN bulunamadı!")
        return

    bot = ApplicationBuilder().token(token).build()
    bot.add_handler(CommandHandler("start", start))

    bot.run_polling(stop_signals=None)

threading.Thread(target=run_bot, daemon=True).start()
