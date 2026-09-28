import os
import threading

from flask import Flask, render_template
from supabase import create_client
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

app = Flask(__name__)

# Supabase bağlantısı
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)


@app.route("/")
def home():
    return render_template("index.html")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    # Kullanıcıyı veritabanına ekle / mevcut kullanıcıyı bul
    result = (
        supabase
        .table("users")
        .upsert(
            {
                "telegram_id": user.id,
                "first_name": user.first_name,
                "last_name": user.last_name,
            },
            on_conflict="telegram_id",
        )
        .execute()
    )

    await update.message.reply_text(
        "🤖 RaTrade Bot'a hoş geldin!\n\n"
        "Mini App'e giriş yapabilirsin. 🚀"
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
