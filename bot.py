import os
from flask import Flask, request
from dotenv import load_dotenv
from groq import Groq
import requests

# =========================
# ENV
# =========================
load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")

print("GROQ LOADED:", GROQ_API_KEY is not None)

# =========================
# AI CLIENT
# =========================
client = Groq(api_key=GROQ_API_KEY)

# =========================
# MEMORY
# =========================
user_history = {}

SYSTEM_PROMPT = "Ти корисний AI-помічник. Відповідай українською."

# =========================
# KEYBOARD
# =========================
MAIN_KEYBOARD = {
    "keyboard": [
        ["🛒 Покупки", "🧊 Запаси"],
        ["🍽️ Що приготувати", "ℹ️ Допомога"]
    ],
    "resize_keyboard": True,
    "is_persistent": True
}

# =========================
# FLASK APP
# =========================
app = Flask(__name__)

def send_message(chat_id, text, reply_markup=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    requests.post(url, json=payload)

@app.route("/")
def home():
    return "Bot is running"

@app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()

    message = data.get("message")
    if not message:
        return "ok"

    chat_id = message["chat"]["id"]
    text = message.get("text", "")

    if not text:
        return "ok"

    # =========================
    # COMMANDS & BUTTONS
    # =========================
    if text == "/start":
        send_message(
            chat_id,
            "Привіт! Я твій домашній помічник 🏠\n\n"
            "Обери дію на клавіатурі або напиши будь-яке запитання — я відповім за допомогою AI.",
            reply_markup=MAIN_KEYBOARD
        )
        return "ok"

    if text == "/menu":
        send_message(chat_id, "Ось головне меню:", reply_markup=MAIN_KEYBOARD)
        return "ok"

    if text == "/help":
        send_message(
            chat_id,
            "ℹ️ Як користуватися ботом:\n\n"
            "🛒 Покупки — список покупок (буде реалізовано)\n"
            "🧊 Запаси — що є вдома (буде реалізовано)\n"
            "🍽️ Що приготувати — ідеї страв на основі запасів\n"
            "ℹ️ Допомога — ця інструкція\n\n"
            "Будь-яке звичайне повідомлення надсилається AI і ти отримаєш відповідь."
        )
        return "ok"

    if text == "🛒 Покупки":
        send_message(chat_id, "Список покупок буде реалізований наступним етапом.")
        return "ok"

    if text == "🧊 Запаси":
        send_message(chat_id, "Облік запасів буде доданий після підключення постійної бази даних.")
        return "ok"

    if text == "🍽️ Що приготувати":
        send_message(chat_id, "Напиши, які продукти зараз є вдома, і я запропоную кілька страв.")
        return "ok"

    if text == "ℹ️ Допомога":
        send_message(
            chat_id,
            "ℹ️ Як користуватися ботом:\n\n"
            "🛒 Покупки — список покупок (буде реалізовано)\n"
            "🧊 Запаси — що є вдома (буде реалізовано)\n"
            "🍽️ Що приготувати — ідеї страв на основі запасів\n"
            "ℹ️ Допомога — ця інструкція\n\n"
            "Будь-яке звичайне повідомлення надсилається AI і ти отримаєш відповідь."
        )
        return "ok"

    # =========================
    # GROQ AI
    # =========================
    if chat_id not in user_history:
        user_history[chat_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    user_history[chat_id].append({"role": "user", "content": text})
    user_history[chat_id] = user_history[chat_id][:1] + user_history[chat_id][-20:]

    try:
        response = client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=user_history[chat_id],
            temperature=0.7
        )

        answer = response.choices[0].message.content

    except Exception as e:
        answer = f"AI error: {str(e)}"

    user_history[chat_id].append({"role": "assistant", "content": answer})

    send_message(chat_id, answer)

    return "ok"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
