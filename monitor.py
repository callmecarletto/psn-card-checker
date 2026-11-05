"""
Eneba PSN Monitor
Controlla le PSN gift card italiane su Eneba e manda un messaggio Telegram
quando una card disponibile è scontata almeno MIN_DISCOUNT % rispetto al taglio.

Variabili d'ambiente:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID  credenziali Telegram (secret del repo)
  MIN_DISCOUNT     sconto minimo in % per avvisare (default 15)
  RUN_MINUTES      0 = un solo controllo; >0 = continua a controllare per N minuti
  CHECK_INTERVAL   secondi tra un controllo e l'altro in modalità loop (default 60)
  MANUAL_RUN       "true" = manda un riepilogo anche se non ci sono offerte
  PRICE_FACTOR     ricarico di Eneba per il paese da cui parte il controllo. Dagli USA
                   (server GitHub) i prezzi risultano ~5,5% più alti che dall'Italia
                   (misurato il 5/11/2026): lì usa 1.055. Da un IP italiano usa 1.
"""

import html
import json
import os
import re
import sys
import time

import requests
from bs4 import BeautifulSoup


BASE_URL = "https://www.eneba.com"
STORE_PATH = "/it/store/psn-gift-cards"
MAX_PAGES = 10

# Questi cookie fanno vedere la pagina come da utente italiano (card IT, prezzi in EUR),
# anche se il controllo parte da un server negli USA.
COOKIES = {"region": "italy", "exchange": "EUR", "lng": "it"}
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}

STATE_FILE = "state.json"
FAIL_ALERT_AFTER = 3  # controlli falliti di fila prima di avvisare che il bot è "cieco"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
MIN_DISCOUNT = float(os.environ.get("MIN_DISCOUNT") or 15)
RUN_MINUTES = float(os.environ.get("RUN_MINUTES") or 0)
CHECK_INTERVAL = float(os.environ.get("CHECK_INTERVAL") or 60)
MANUAL_RUN = (os.environ.get("MANUAL_RUN") or "").lower() == "true"
PRICE_FACTOR = float(os.environ.get("PRICE_FACTOR") or 1)
APPROX = "≈ " if abs(PRICE_FACTOR - 1) > 1e-9 else ""

PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€")
FACE_RE = re.compile(r"Card\s+(\d+(?:[.,]\d+)?)\s*EUR", re.IGNORECASE)


# ---------------------------------------------------------------- Telegram

def send_telegram(message):
    if not BOT_TOKEN or not CHAT_ID:
        print("Telegram non configurato. Messaggio:\n" + message)
        return
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data={
                "chat_id": CHAT_ID,
                "text": message,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if response.ok:
            print("Messaggio Telegram inviato.")
        else:
            print(f"Errore Telegram: {response.status_code} - {response.text}")
    except requests.RequestException as error:
        print(f"Errore Telegram: {error}")


# ---------------------------------------------------------------- Parsing

def parse_euro(text):
    return float(text.replace(".", "").replace(",", "."))


def fmt_euro(value):
    return f"{value:,.2f} €".replace(",", "X").replace(".", ",").replace("X", ".")


def card_container(link):
    """Risale dal link al contenitore più grande che contiene un solo prodotto."""
    node = link
    while node.parent is not None:
        hrefs = {a.get("href") for a in node.parent.select("a[title][href]")}
        if len(hrefs) > 1:
            break
        node = node.parent
    return node


def parse_cards(page_html):
    soup = BeautifulSoup(page_html, "html.parser")
    cards = []
    seen = set()

    for link in soup.select("a[title][href]"):
        title = link.get("title", "")
        href = link.get("href", "")
        if "Network Card" not in title or href in seen:
            continue
        seen.add(href)

        face_match = FACE_RE.search(title)
        if not face_match:
            continue  # card non in euro, la ignoro
        face = float(face_match.group(1).replace(".", "").replace(",", "."))

        container = card_container(link)
        text = container.get_text(" ", strip=True).replace("\xa0", " ")
        region_tag = container.select_one("div[title]")

        card = {
            "title": title,
            "url": BASE_URL + href if href.startswith("/") else href,
            "face": face,
            "region": region_tag.get("title") if region_tag else "",
            "in_stock": False,
            "price": None,
            "discount": None,
        }

        if "Esaurito" not in text:
            # Blocco prezzo: "Da [prezzo pieno] [-x%] [prezzo attuale]" e poi il cashback.
            start = text.find("Da ")
            block = text[start:] if start >= 0 else text
            block = re.split(r"Cashback|Aggiungi al carrello", block)[0]
            prices = [parse_euro(p) for p in PRICE_RE.findall(block)]
            if prices:
                card["raw_price"] = prices[-1]
                card["price"] = round(prices[-1] / PRICE_FACTOR, 2)
                card["in_stock"] = True
                card["discount"] = round((1 - card["price"] / face) * 100, 1)

        cards.append(card)

    return cards


def fetch_all_cards(session):
    cards = []
    for page in range(1, MAX_PAGES + 1):
        url = BASE_URL + STORE_PATH + (f"?page={page}" if page > 1 else "")
        response = session.get(url, timeout=30)
        response.raise_for_status()
        page_cards = parse_cards(response.text)
        if not page_cards:
            break
        new = [c for c in page_cards if c["url"] not in {x["url"] for x in cards}]
        if not new:
            break  # pagina oltre la fine che ripete l'ultima
        cards.extend(new)
    return cards


# ---------------------------------------------------------------- Stato

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as file:
            data = json.load(file)
        if "eneba" in data:
            return data["eneba"], False
    return {"cards": {}, "fail_count": 0, "blind_alert_sent": False}, True


def save_state(state):
    with open(STATE_FILE, "w") as file:
        json.dump({"eneba": state}, file, indent=2, ensure_ascii=False)


# ---------------------------------------------------------------- Messaggi

def fmt_pct(value):
    return f"{value:g}".replace(".", ",")


def short_name(card):
    return f"PSN {card['face']:g} € ({card['region'] or 'IT'})"


def deal_line(card):
    return (
        f"• <b>{html.escape(short_name(card))}</b> a <b>{APPROX}{fmt_euro(card['price'])}</b> "
        f"(-{fmt_pct(card['discount'])}%)\n  {html.escape(card['url'])}"
    )


def deal_message(card, previous_price=None):
    head = "🔥 <b>PSN SCONTATA SU ENEBA</b>"
    if previous_price is not None:
        head = "🔥 <b>PSN: IL PREZZO È SCESO ANCORA</b>"
    lines = [
        head,
        "",
        f"<b>{html.escape(short_name(card))}</b>",
        f"Prezzo: <b>{APPROX}{fmt_euro(card['price'])}</b> invece di {fmt_euro(card['face'])} "
        f"(<b>-{fmt_pct(card['discount'])}%</b>)",
    ]
    if previous_price is not None:
        lines.append(f"Prima: {fmt_euro(previous_price)}")
    lines += ["", f"👉 {html.escape(card['url'])}"]
    return "\n".join(lines)


# ---------------------------------------------------------------- Controllo

def is_deal(card):
    return card["in_stock"] and card["discount"] is not None and card["discount"] >= MIN_DISCOUNT


def check_once(session, state, first_run):
    try:
        cards = fetch_all_cards(session)
        if not cards:
            raise RuntimeError("nessuna card trovata (pagina cambiata o bloccata?)")
    except Exception as error:
        state["fail_count"] = state.get("fail_count", 0) + 1
        print(f"Controllo fallito ({state['fail_count']} di fila): {error}")
        if state["fail_count"] >= FAIL_ALERT_AFTER and not state.get("blind_alert_sent"):
            send_telegram(
                "⚠️ <b>Eneba PSN Monitor non riesce a leggere la pagina</b>\n\n"
                f"Ultimo errore: {html.escape(str(error))[:300]}\n"
                "Finché non si sblocca non posso avvisarti degli sconti."
            )
            state["blind_alert_sent"] = True
        return None

    if state.get("blind_alert_sent"):
        send_telegram("✅ Eneba PSN Monitor è tornato a leggere la pagina.")
    state["fail_count"] = 0
    state["blind_alert_sent"] = False

    in_stock = [c for c in cards if c["in_stock"]]
    print(f"{len(cards)} card trovate, {len(in_stock)} disponibili.")
    for c in sorted(in_stock, key=lambda c: -c["discount"])[:5]:
        print(f"  {short_name(c)}: {fmt_euro(c['price'])} (sconto {fmt_pct(c['discount'])}%)")

    known = state.setdefault("cards", {})
    new_deals = []

    for card in cards:
        old = known.get(card["url"], {})
        alerted_price = old.get("alerted_price")

        if is_deal(card):
            if alerted_price is None or card["price"] < alerted_price - 0.009:
                new_deals.append((card, alerted_price))
                alerted_price = card["price"]
        else:
            alerted_price = None  # se torna in offerta in futuro, riavviso

        known[card["url"]] = {
            "title": card["title"],
            "price": card["price"],
            "discount": card["discount"],
            "in_stock": card["in_stock"],
            "alerted_price": alerted_price,
        }

    if first_run:
        deals_now = [c for c, _ in new_deals]
        text = (
            "🟢 <b>Eneba PSN Monitor attivo</b>\n\n"
            f"Controllo le PSN card italiane e ti scrivo quando una disponibile è scontata "
            f"almeno del <b>{fmt_pct(MIN_DISCOUNT)}%</b>.\n"
            f"Card trovate: {len(cards)} ({len(in_stock)} disponibili)."
        )
        if deals_now:
            text += "\n\n<b>Già in offerta adesso:</b>\n" + "\n".join(deal_line(c) for c in deals_now)
        send_telegram(text)
        state["_activation_sent_now"] = True
    else:
        for card, previous in new_deals:
            send_telegram(deal_message(card, previous))

    return cards


def main():
    state, first_run = load_state()
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.update(COOKIES)

    deadline = time.time() + RUN_MINUTES * 60
    last_cards = None

    while True:
        cards = check_once(session, state, first_run)
        if cards is not None:
            last_cards = cards
            first_run = False
        save_state(state)

        if time.time() + CHECK_INTERVAL >= deadline:
            break
        time.sleep(CHECK_INTERVAL)

    activation_sent = state.pop("_activation_sent_now", False)
    save_state(state)

    if MANUAL_RUN and last_cards is not None and not activation_sent:
        deals = [c for c in last_cards if is_deal(c)]
        in_stock = [c for c in last_cards if c["in_stock"]]
        if deals:
            body = "<b>Sopra soglia adesso:</b>\n" + "\n".join(deal_line(c) for c in deals)
        else:
            best = max(in_stock, key=lambda c: c["discount"]) if in_stock else None
            body = "Nessuna card sopra soglia."
            if best:
                body += f"\nMigliore adesso: {deal_line(best)}"
        send_telegram(
            f"✅ <b>Controllo Eneba completato</b> (soglia {fmt_pct(MIN_DISCOUNT)}%)\n\n{body}"
        )

    if last_cards is None:
        sys.exit(1)  # nessun controllo riuscito: il run risulta fallito su GitHub


if __name__ == "__main__":
    main()
