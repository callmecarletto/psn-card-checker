import json
import os
import hashlib
import requests

from playwright.sync_api import sync_playwright


URL = "https://cyberleek.ar.io/"
STATE_FILE = "state.json"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram non configurato.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": False
        },
        timeout=20
    )


def get_leeks_content():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(
            viewport={
                "width": 1440,
                "height": 1200
            }
        )

        page.goto(
            URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(10000)

        body_text = page.locator("body").inner_text()

        browser.close()

        try:
            start = body_text.index("LEEKS")
            end = body_text.index("POLL", start)

            return body_text[start:end].strip()

        except ValueError:
            return body_text


def get_hash(content):
    return hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()


def load_state():
    if not os.path.exists(STATE_FILE):
        return None

    with open(STATE_FILE, "r") as file:
        data = json.load(file)

    return data.get("hash")


def save_state(current_hash):
    with open(STATE_FILE, "w") as file:
        json.dump(
            {
                "hash": current_hash
            },
            file,
            indent=2
        )


content = get_leeks_content()

print("Contenuto LEEKS:")
print(content)

current_hash = get_hash(content)
previous_hash = load_state()


if previous_hash is None:

    print("Prima esecuzione.")

    save_state(current_hash)

    send_telegram(
        "🟢 Cyberleek Monitor attivato.\n\n"
        "Da ora controllerò la sezione LEEKS."
    )


elif current_hash != previous_hash:

    print("CAMBIAMENTO RILEVATO!")

    send_telegram(
        "🚨 NUOVO AGGIORNAMENTO CYBERLEEK\n\n"
        "La sezione LEEKS è cambiata.\n\n"
        "https://cyberleek.ar.io/"
    )

    save_state(current_hash)


else:

    print("Nessun cambiamento.")
