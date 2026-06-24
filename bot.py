import os
from flask import Flask, request
from threading import Thread
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters
from groq import Groq

# =========================
# ENV
# =========================
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

print("GROQ LOADED:", GROQ_API_KEY is not None)

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
Відповідай українською, чітко і без вигадок.
"""

# =========================
# TELEGRAM BOT (no polling)
# =========================
app_telegram = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Привіт 👋 Я AI-бот з пам'яттю.")

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_chat.id
    text = update.message.text

    if user_id not in user_history:
        user_history[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    user_history[user_id].append({"role": "user", "content": text})

    user_history[user_id] = user_history[user_id][:1] + user_history[user_id][-20:]

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=user_history[user_id],
            temperature=0.7
        )
        answer = response.choices[0].message.content
    except Exception as e:
        answer = f"AI error: {str(e)}"

    user_history[user_id].append({"role": "assistant", "content": answer})

    await update.message.reply_text(answer)

app_telegram.add_handler(CommandHandler("start", start))
app_telegram.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

# =========================
# FLASK SERVER (Render entry point)
# =========================
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "Bot is running"

@flask_app.route(f"/webhook/{TELEGRAM_TOKEN}", methods=["POST"])
def webhook():
    update = Update.de_json(request.get_json(force=True), app_telegram.bot)
    app_telegram.update_queue.put_nowait(update)
    return "ok"

def run_flask():
    port = int(os.environ.get("PORT", 10000))
    flask_app.run(host="0.0.0.0", port=port)

# =========================
# START
# =========================
if __name__ == "__main__":
    print("Bot starting...")

    app_telegram.initialize()
    app_telegram.start()

    Thread(target=run_flask).start()

    print("Bot is running...")