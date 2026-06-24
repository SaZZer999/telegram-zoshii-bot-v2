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
# FLASK APP
# =========================
app = Flask(__name__)

def send_message(chat_id, text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text})

@app.route("/")
def home():
    return "Bot is running"

@app.route(f"/webhook/{TOKEN}", methods=["POST"])
def webhook():
    data = request.get_json()

    if "message" not in data:
        return "ok"

    chat_id = data["message"]["chat"]["id"]
    text = data["message"].get("text", "")

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