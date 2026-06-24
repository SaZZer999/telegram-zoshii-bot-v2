import os
from threading import Thread
from flask import Flask

# FIX для Windows SSL
os.environ["PYTHONHTTPSVERIFY"] = "0"

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters
)

from groq import Groq

# =========================
# ENV
# =========================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

print("GROQ LOADED:", GROQ_API_KEY is not None)

# =========================
# WEB SERVER FOR RENDER
# =========================
web_app = Flask(__name__)

@web_app.route("/")
def home():
    return "Bot is running"

def run_web():
    port = int(os.environ.get("PORT", 10000))
    web_app.run(host="0.0.0.0", port=port)

# =========================
# GROQ CLIENT
# =========================
client = Groq(api_key=GROQ_API_KEY)

# =========================
# MEMORY
# =========================
user_history = {}

SYSTEM_PROMPT = """
Ти уважний, логічний і корисний AI-помічник.

Правила:
- Відповідай українською мовою
- Давай чіткі та структуровані відповіді
- Якщо не знаєш — прямо скажи
- Не вигадуй факти
- Якщо питання неясне — уточнюй
- Пам’ятай контекст розмови
"""

# =========================
# START
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привіт 👋 Я AI-бот з пам’яттю. Напиши щось."
    )

# =========================
# HANDLE MESSAGE
# =========================
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_chat.id
    user_text = update.message.text

    if user_id not in user_history:
        user_history[user_id] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]

    user_history[user_id].append(
        {"role": "user", "content": user_text}
    )

    user_history[user_id] = (
        user_history[user_id][:1] + user_history[user_id][-20:]
    )

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=user_history[user_id],
            temperature=0.7
        )

        answer = response.choices[0].message.content

        user_history[user_id].append(
            {"role": "assistant", "content": answer}
        )

    except Exception as e:
        answer = f"AI error: {str(e)}"

    await update.message.reply_text(answer)

# =========================
# BOT START
# =========================
app = (
    ApplicationBuilder()
    .token(TELEGRAM_TOKEN)
    .concurrent_updates(False)
    .build()
)

app.add_handler(CommandHandler("start", start))
app.add_handler(
    MessageHandler(
        filters.TEXT & ~filters.COMMAND,
        handle_message
    )
)

# Запускаємо вебсервер для Render
Thread(target=run_web, daemon=True).start()

print("Бот запущений...")
app.run_polling()