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
  MIN_DROP         % del taglio di cui deve scendere ancora il prezzo per un nuovo avviso (default 2)
  REARM_MINUTES    minuti fuori offerta dopo cui la stessa card può essere risegnalata (default 60)
  PRICE_FACTOR     ricarico di Eneba per il paese da cui parte il controllo. Dagli USA
                   (server GitHub) i prezzi risultano ~5,5% più alti che dall'Italia
                   (misurato il 5/11/2026): lì usa 1.055. Da un IP italiano usa 1.
"""

import base64
import hashlib
import html
import json
import os
import re
import subprocess
import sys
import time

import requests
from bs4 import BeautifulSoup
from cryptography.fernet import Fernet, InvalidToken


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

STATE_FILE = os.environ.get("STATE_FILE") or "state.json"
FAIL_ALERT_AFTER = 3  # controlli falliti di fila prima di avvisare che il bot è "cieco"

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
MIN_DISCOUNT = float(os.environ.get("MIN_DISCOUNT") or 15)
RUN_MINUTES = float(os.environ.get("RUN_MINUTES") or 0)
CHECK_INTERVAL = float(os.environ.get("CHECK_INTERVAL") or 60)
MANUAL_RUN = (os.environ.get("MANUAL_RUN") or "").lower() == "true"
PRICE_FACTOR = float(os.environ.get("PRICE_FACTOR") or 1)
MIN_DROP = float(os.environ.get("MIN_DROP") or 2)
REARM_MINUTES = float(os.environ.get("REARM_MINUTES") or 60)
COMMANDS = os.environ.get("COMMANDS", "1") == "1"   # gestisce /start, approvazioni ecc.
PERSIST_GIT = os.environ.get("PERSIST_GIT") == "1"    # salva subito su GitHub quando cambiano gli iscritti
MAX_PENDING = 20
APPROX = "≈ " if abs(PRICE_FACTOR - 1) > 1e-9 else ""

PRICE_RE = re.compile(r"(\d{1,3}(?:\.\d{3})*,\d{2})\s*€")
FACE_RE = re.compile(r"Card\s+(\d+(?:[.,]\d+)?)\s*EUR", re.IGNORECASE)


# ---------------------------------------------------------------- Telegram

def send_telegram(message, chat_id=None, buttons=None):
    """Manda un messaggio. Senza chat_id va al proprietario del bot (TELEGRAM_CHAT_ID)."""
    chat_id = chat_id or CHAT_ID
    if not BOT_TOKEN or not chat_id:
        print("Telegram non configurato. Messaggio:\n" + message)
        return False
    data = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    if buttons:
        data["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
            data=data,
            timeout=20,
        )
        if response.ok:
            print("Messaggio Telegram inviato.")
            return True
        print(f"Errore Telegram: {response.status_code} - {response.text}")
    except requests.RequestException as error:
        print(f"Errore Telegram: {error}")
    return False


def internet_ok():
    """True se si raggiunge Telegram: serve a distinguere 'niente internet' da 'Eneba bloccata'."""
    try:
        requests.head("https://api.telegram.org", timeout=8)
        return True
    except requests.RequestException:
        return False


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

def _fernet():
    # Il repo è pubblico: gli iscritti (chat ID e nomi) vengono salvati cifrati
    # con una chiave ricavata dal token del bot, che resta segreto.
    key = hashlib.sha256(b"psn-subscribers:" + (BOT_TOKEN or "").encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def load_subscribers(state):
    blob = state.get("subscribers_enc")
    if blob and BOT_TOKEN:
        try:
            return json.loads(_fernet().decrypt(blob.encode()))
        except (InvalidToken, ValueError):
            print("Iscritti illeggibili (token del bot cambiato?): riparto da zero.")
    return {"approved": {}, "pending": {}}


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as file:
            data = json.load(file)
        if "eneba" in data:
            state = data["eneba"]
            state["_subs"] = load_subscribers(state)
            return state, False
    return {"cards": {}, "fail_count": 0, "blind_alert_sent": False,
            "_subs": {"approved": {}, "pending": {}}}, True


def save_state(state):
    data = {k: v for k, v in state.items() if not k.startswith("_")}
    if BOT_TOKEN and "_subs" in state:
        plain = json.dumps(state["_subs"], ensure_ascii=False).encode()
        data["subscribers_enc"] = _fernet().encrypt(plain).decode()
    with open(STATE_FILE, "w") as file:
        json.dump({"eneba": data}, file, indent=2, ensure_ascii=False)


def persist_to_github():
    """Committa subito state.json (usato quando cambiano gli iscritti)."""
    if not PERSIST_GIT:
        return
    ref = os.environ.get("GITHUB_REF_NAME", "main")
    cmds = [
        ["git", "add", "state.json"],
        ["git", "commit", "-q", "-m", "Update subscribers"],
        ["git", "pull", "-q", "--rebase", "-X", "theirs", "origin", ref],
        ["git", "push", "-q", "origin", f"HEAD:{ref}"],
    ]
    for cmd in cmds:
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0 and cmd[1] != "commit":
            print(f"Salvataggio su GitHub non riuscito ({' '.join(cmd[:2])}): {result.stderr[:200]}")
            return


# ---------------------------------------------------------------- Iscritti

def subscriber_ids(state):
    return list(state.get("_subs", {}).get("approved", {}).keys())


def send_to_all(state, message):
    """Manda al proprietario e a tutti gli iscritti. Ritorna l'esito per il proprietario."""
    ok = send_telegram(message)
    for chat_id in subscriber_ids(state):
        send_telegram(message, chat_id=chat_id)
    return ok


def tg_call(method, **params):
    try:
        r = requests.post(f"https://api.telegram.org/bot{BOT_TOKEN}/{method}", data=params, timeout=20)
        return r.json()
    except (requests.RequestException, ValueError) as error:
        print(f"Errore Telegram ({method}): {error}")
        return None


def display_name(user):
    name = " ".join(x for x in [user.get("first_name"), user.get("last_name")] if x) or "Senza nome"
    if user.get("username"):
        name += f" (@{user['username']})"
    return name


def welcome_text():
    return (
        "✅ <b>Sei dentro!</b>\n\n"
        "Ti scrivo qui quando una PSN card italiana su Eneba è scontata almeno del "
        f"<b>{fmt_pct(MIN_DISCOUNT)}%</b>. Le offerte finiscono in fretta: quando arriva "
        "l'avviso conviene aprire subito il link.\n\n"
        "Per non ricevere più avvisi scrivi /stop."
    )


def handle_owner_command(state, text):
    subs = state["_subs"]
    parts = text.split()
    cmd = parts[0].split("@")[0].lower() if parts else ""
    arg = parts[1] if len(parts) > 1 else ""
    if cmd == "/approva" and arg:
        return approve(state, arg)
    if cmd in ("/rifiuta", "/rimuovi") and arg:
        name = subs["pending"].pop(arg, None) or subs["approved"].pop(arg, None)
        if not name:
            send_telegram(f"Non trovo nessuno con ID {html.escape(arg)}.")
            return False
        send_telegram(f"🗑 Rimosso: {html.escape(name)}")
        return True
    if cmd == "/lista":
        lines = ["<b>Iscritti</b>"] + [f"• {html.escape(n)} — <code>{i}</code>" for i, n in subs["approved"].items()]
        if not subs["approved"]:
            lines.append("nessuno")
        if subs["pending"]:
            lines += ["", "<b>In attesa</b>"] + [f"• {html.escape(n)} — <code>{i}</code>" for i, n in subs["pending"].items()]
        lines += ["", "Rimuovere qualcuno: /rimuovi ID"]
        send_telegram("\n".join(lines))
        return False
    send_telegram(
        "Comandi:\n/lista — chi riceve gli avvisi\n/approva ID — approva una richiesta\n"
        "/rifiuta ID — rifiuta una richiesta\n/rimuovi ID — toglie un iscritto\n\n"
        "Per invitare qualcuno: digli di aprire questo bot su Telegram e premere Avvia (/start)."
    )
    return False


def approve(state, chat_id):
    subs = state["_subs"]
    name = subs["pending"].pop(chat_id, None)
    if not name:
        send_telegram(f"Nessuna richiesta in attesa con ID {html.escape(chat_id)}.")
        return False
    subs["approved"][chat_id] = name
    send_telegram(f"✅ Approvato: {html.escape(name)}")
    send_telegram(welcome_text(), chat_id=chat_id)
    return True


def handle_guest_message(state, chat_id, user, text):
    subs = state["_subs"]
    cmd = text.split()[0].split("@")[0].lower() if text.split() else ""
    name = display_name(user)
    if cmd == "/stop":
        removed = subs["approved"].pop(chat_id, None) or subs["pending"].pop(chat_id, None)
        send_telegram("👋 Fatto, non riceverai più avvisi. Per tornare scrivi /start.", chat_id=chat_id)
        if removed:
            send_telegram(f"👋 {html.escape(removed)} si è disiscritto.")
        return bool(removed)
    if chat_id in subs["approved"]:
        send_telegram("Sei già iscritto: ti scrivo appena c'è un'offerta. Per uscire scrivi /stop.", chat_id=chat_id)
        return False
    if chat_id in subs["pending"]:
        send_telegram("La tua richiesta è in attesa di approvazione.", chat_id=chat_id)
        return False
    if cmd != "/start":
        send_telegram("Questo bot avvisa degli sconti sulle PSN card su Eneba. Scrivi /start per chiedere di ricevere gli avvisi.", chat_id=chat_id)
        return False
    if len(subs["pending"]) >= MAX_PENDING:
        return False
    subs["pending"][chat_id] = name
    send_telegram("📨 Richiesta inviata. Ti scrivo appena viene approvata.", chat_id=chat_id)
    send_telegram(
        f"🔔 <b>Nuova richiesta</b>\n\n{html.escape(name)} vuole ricevere gli avvisi PSN.",
        buttons=[[{"text": "✅ Approva", "callback_data": f"ok:{chat_id}"},
                  {"text": "❌ Rifiuta", "callback_data": f"no:{chat_id}"}]],
    )
    return True


def process_updates(state):
    """Legge i messaggi arrivati al bot (iscrizioni e comandi). Ritorna True se gli iscritti sono cambiati."""
    if not COMMANDS or not BOT_TOKEN or not CHAT_ID:
        return False
    result = tg_call("getUpdates", offset=state.get("tg_offset", 0), timeout=0,
                     allowed_updates=json.dumps(["message", "callback_query"]))
    if not result or not result.get("ok"):
        return False
    changed = False
    for update in result.get("result", []):
        state["tg_offset"] = update["update_id"] + 1
        try:
            if "callback_query" in update:
                query = update["callback_query"]
                tg_call("answerCallbackQuery", callback_query_id=query["id"])
                if str(query["from"]["id"]) != str(CHAT_ID):
                    continue
                action, _, target = (query.get("data") or "").partition(":")
                if action == "ok":
                    changed |= approve(state, target)
                elif action == "no":
                    changed |= handle_owner_command(state, f"/rifiuta {target}")
                continue
            msg = update.get("message") or {}
            chat = msg.get("chat", {})
            if chat.get("type") != "private":
                continue
            chat_id = str(chat["id"])
            text = (msg.get("text") or "").strip()
            if chat_id == str(CHAT_ID):
                changed |= handle_owner_command(state, text)
            else:
                changed |= handle_guest_message(state, chat_id, msg.get("from", {}), text)
        except Exception as error:  # un messaggio strano non deve fermare il bot
            print(f"Messaggio Telegram non gestito: {error}")
    return changed


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
        if isinstance(error, (requests.ConnectionError, requests.Timeout)) and not internet_ok():
            print("Nessuna connessione a internet: salto questo controllo.")
            return None
        state["fail_count"] = state.get("fail_count", 0) + 1
        print(f"Controllo fallito ({state['fail_count']} di fila): {error}")
        if state["fail_count"] >= FAIL_ALERT_AFTER and not state.get("blind_alert_sent"):
            state["blind_alert_sent"] = send_telegram(
                "⚠️ <b>Eneba PSN Monitor non riesce a leggere la pagina</b>\n\n"
                f"Ultimo errore: {html.escape(str(error))[:300]}\n"
                "Finché non si sblocca non posso avvisarti degli sconti."
            )
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

    now = time.time()
    for card in cards:
        old = known.get(card["url"], {})
        alerted_price = old.get("alerted_price")
        out_since = old.get("out_since")

        if is_deal(card):
            out_since = None
            # Nuovo avviso solo per offerte nuove o ribassi veri (i venditori si
            # scavalcano di 1 centesimo alla volta: quelli non contano).
            min_step = card["face"] * MIN_DROP / 100
            if alerted_price is None or card["price"] <= alerted_price - min_step:
                new_deals.append((card, alerted_price))
                alerted_price = card["price"]
        elif alerted_price is not None:
            # Fuori offerta: la "dimentico" solo dopo REARM_MINUTES, così se
            # sparisce e ricompare subito non ti arriva lo stesso avviso due volte.
            out_since = out_since or now
            if now - out_since >= REARM_MINUTES * 60:
                alerted_price, out_since = None, None

        known[card["url"]] = {
            "title": card["title"],
            "price": card["price"],
            "discount": card["discount"],
            "in_stock": card["in_stock"],
            "alerted_price": alerted_price,
            "out_since": out_since,
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
            if not send_to_all(state, deal_message(card, previous)):
                known[card["url"]]["alerted_price"] = previous  # riprovo al prossimo controllo

    return cards


def main():
    state, first_run = load_state()
    session = requests.Session()
    session.headers.update(HEADERS)
    session.cookies.update(COOKIES)

    deadline = time.time() + RUN_MINUTES * 60
    last_cards = None

    while True:
        if process_updates(state):
            save_state(state)
            persist_to_github()
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
