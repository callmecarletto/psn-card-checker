import json
import os
import hashlib
import requests

from playwright.sync_api import sync_playwright


SITES = [
    {
        "name": "Cyberleek AR",
        "url": "https://cyberleek.ar.io/"
    },
    {
        "name": "Cyberleek Turbo Gateway",
        "url": "https://cyberleek.turbo-gateway.com/"
    },
    {
        "name": "Cyberleek Vilenarios",
        "url": "https://cyberleek.vilenarios.com/"
    }
]

STATE_FILE = "state.json"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram non configurato.")
        return

    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"

    response = requests.post(
        url,
        data={
            "chat_id": CHAT_ID,
            "text": message,
            "disable_web_page_preview": False
        },
        timeout=20
    )

    if response.ok:
        print("Messaggio Telegram inviato.")
    else:
        print(f"Errore Telegram: {response.status_code} - {response.text}")


def get_leeks_content(site_url):
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)

        page = browser.new_page(
            viewport={
                "width": 1440,
                "height": 1200
            }
        )

        page.goto(
            site_url,
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
        return {}

    with open(STATE_FILE, "r") as file:
        data = json.load(file)

    if "sites" in data:
        return data["sites"]

    if "hash" in data:
        return {
            SITES[0]["url"]: data["hash"]
        }

    return {}


def save_state(state):
    with open(STATE_FILE, "w") as file:
        json.dump(
            {
                "sites": state
            },
            file,
            indent=2
        )


state = load_state()
updated_state = state.copy()
new_sites = []
changes = []
manual_run = os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"


for site in SITES:
    site_name = site["name"]
    site_url = site["url"]

    print(f"Controllo {site_name}: {site_url}")

    try:
        content = get_leeks_content(site_url)
    except Exception as error:
        print(f"Errore durante il controllo di {site_name}: {error}")
        continue

    print("Contenuto LEEKS:")
    print(content)

    current_hash = get_hash(content)
    previous_hash = state.get(site_url)

    if previous_hash is None:
        print(f"Prima esecuzione per {site_name}.")
        updated_state[site_url] = current_hash
        new_sites.append(site)

    elif current_hash != previous_hash:
        print(f"CAMBIAMENTO RILEVATO per {site_name}!")
        updated_state[site_url] = current_hash
        changes.append(site)

    else:
        print(f"Nessun cambiamento per {site_name}.")


if new_sites:
    site_list = "\n".join(
        f"- {site['name']}: {site['url']}"
        for site in new_sites
    )

    send_telegram(
        "🟢 Cyberleek Monitor attivato/aggiornato.\n\n"
        "Da ora controllerò la sezione LEEKS su questi siti:\n"
        f"{site_list}"
    )


for site in changes:
    send_telegram(
        "🚨 NUOVO AGGIORNAMENTO CYBERLEEK\n\n"
        f"Sito: {site['name']}\n"
        "La sezione LEEKS è cambiata.\n\n"
        f"{site['url']}"
    )


if manual_run and not new_sites and not changes:
    send_telegram(
        "✅ Controllo Cyberleek completato.\n\n"
        "Nessun cambiamento rilevato nella sezione LEEKS."
    )


save_state(updated_state)
