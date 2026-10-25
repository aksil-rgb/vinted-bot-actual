#!/usr/bin/env python3
"""
Bot Telegram : alertes "bonnes affaires" Vinted
===============================================

Installation :
    pip install requests curl_cffi
    (curl_cffi est optionnel mais fortement recommandé : il imite un vrai Chrome)

Configuration :
    1. Renseigne TELEGRAM_TOKEN et TELEGRAM_CHAT_ID ci-dessous
       (ou via les variables d'environnement du même nom).
       Ton chat ID : écris au bot, puis ouvre
       https://api.telegram.org/bot<TON_TOKEN>/getUpdates
    2. Modifie la liste SEARCHES.
    3. Lance :  python vinted_bot.py

Conseils :
    - Lance-le depuis chez toi (PC, Raspberry Pi), pas depuis un serveur :
      les IP de datacenters sont bloquées d'office par Vinted.
    - Garde des délais raisonnables. En cas de blocage (403/429), le bot
      fait une pause de plus en plus longue au lieu d'insister.
"""

import html
import json
import logging
import os
import random
import re
import time
from pathlib import Path

import requests

try:
    from curl_cffi import requests as cffi_requests  # empreinte TLS de Chrome
except ImportError:
    cffi_requests = None

# ───────────────────────────── CONFIGURATION ─────────────────────────────

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "8694226548:AAE3aeMPbl27AC-v5tcA903iDC0YPfn4OME")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "hunter_vinted_bot")

DOMAIN = "www.vinted.fr"  # www.vinted.be, www.vinted.es, ...

SEARCHES = [
    {
        "name": "Nike Tech Fleece",
        "search_text": "nike tech fleece",
        "price_to": 35,
        "exclude": ["enfant", "lot", "défaut"],  # mots interdits dans le titre
    },
    {
        "name": "Switch OLED",
        "search_text": "nintendo switch oled",
        "price_from": 80,
        "price_to": 150,
        "exclude": ["pour pièces", "défectueux", "boîte vide"],
        # "extra": {"catalog_ids": "3025", "brand_ids": "53"},  # filtres Vinted (optionnel)
    },
]

CYCLE_DELAY = (45, 90)    # pause entre deux tours complets (secondes)
SEARCH_DELAY = (5, 12)    # pause entre deux recherches d'un même tour
FIRST_BLOCK_PAUSE = 300   # pause initiale si Vinted bloque (5 min), doublée à chaque fois
MAX_BLOCK_PAUSE = 3600    # ...jusqu'à 1 h maximum

# ─────────────────────────────────────────────────────────────────────────

STATE_FILE = Path(__file__).with_name("seen_items.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "fr-FR,fr;q=0.9,en;q=0.8"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("vinted-bot")


# ───────────────────────────── VINTED ─────────────────────────────

class Blocked(Exception):
    """Vinted refuse les requêtes (403 / 429)."""


class VintedClient:
    def __init__(self):
        self.session = None
        self.refresh()

    def refresh(self):
        """(Re)crée une session et récupère les cookies comme un navigateur."""
        if cffi_requests:
            self.session = cffi_requests.Session(impersonate="chrome")
        else:
            self.session = requests.Session()
            self.session.headers["User-Agent"] = USER_AGENT
        r = self.session.get(
            f"https://{DOMAIN}/",
            headers={"Accept-Language": ACCEPT_LANGUAGE},
            timeout=20,
        )
        if r.status_code in (403, 429):
            raise Blocked(r.status_code)
        log.info("Session Vinted initialisée (HTTP %s)", r.status_code)

    def search(self, params):
        url = f"https://{DOMAIN}/api/v2/catalog/items"
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": ACCEPT_LANGUAGE,
            "Referer": f"https://{DOMAIN}/catalog",
        }
        r = self.session.get(url, params=params, headers=headers, timeout=20)
        if r.status_code == 401:  # cookies expirés : on repart d'une session neuve
            self.refresh()
            r = self.session.get(url, params=params, headers=headers, timeout=20)
        if r.status_code in (403, 429):
            raise Blocked(r.status_code)
        r.raise_for_status()
        return r.json().get("items", [])


# ───────────────────────────── OUTILS ─────────────────────────────

def parse_price(item):
    """Retourne (montant, devise) quel que soit le format renvoyé par Vinted."""
    p = item.get("price")
    if isinstance(p, dict):
        amount, cur = p.get("amount"), p.get("currency_code", "EUR")
    else:
        amount, cur = p, item.get("currency", "EUR")
    try:
        return float(amount), cur
    except (TypeError, ValueError):
        return 0.0, cur


def is_excluded(title, words):
    t = (title or "").lower()
    return any(re.search(rf"\b{re.escape(w.lower())}\b", t) for w in words)


def build_caption(item, search_name):
    title = html.escape(item.get("title") or "Sans titre")
    amount, cur = parse_price(item)
    symbol = "€" if cur == "EUR" else cur
    details = [item.get("brand_title"), item.get("size_title"), item.get("status")]
    details = [html.escape(str(d)) for d in details if d]
    url = item.get("url") or f"https://{DOMAIN}/items/{item['id']}"

    lines = [f"🆕 <b>{title}</b>", f"💶 <b>{amount:.2f} {symbol}</b>"]
    if details:
        lines.append(" · ".join(details))
    lines.append(f"🔎 {html.escape(search_name)}")
    lines.append(f'<a href="{html.escape(url)}">Voir sur Vinted</a>')
    return "\n".join(lines)


# ───────────────────────────── TELEGRAM ─────────────────────────────

def telegram(method, **payload):
    payload["chat_id"] = TELEGRAM_CHAT_ID
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
            json=payload,
            timeout=20,
        )
    except requests.RequestException as e:
        log.warning("Telegram injoignable (%s)", type(e).__name__)
        return False
    if not r.ok:
        log.warning("Telegram a refusé l'envoi (HTTP %s) : %s", r.status_code, r.text[:150])
    return r.ok


def notify(item, search_name):
    caption = build_caption(item, search_name)
    photo = (item.get("photo") or {}).get("url")
    sent = False
    if photo:
        sent = telegram("sendPhoto", photo=photo, caption=caption, parse_mode="HTML")
    if not sent:
        telegram("sendMessage", text=caption, parse_mode="HTML")


# ───────────────────────────── ÉTAT (anti-doublons) ─────────────────────────────

def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return set(data.get("seen", [])), set(data.get("initialized", []))
    except (OSError, ValueError):
        return set(), set()


def save_state(seen, initialized):
    recent = sorted(seen)[-5000:]  # les ids Vinted augmentent : on garde les plus récents
    STATE_FILE.write_text(
        json.dumps({"seen": recent, "initialized": sorted(initialized)}),
        encoding="utf-8",
    )


# ───────────────────────────── BOUCLE PRINCIPALE ─────────────────────────────

def run_cycle(client, seen, initialized):
    for i, s in enumerate(SEARCHES):
        if i:
            time.sleep(random.uniform(*SEARCH_DELAY))

        params = {
            "search_text": s["search_text"],
            "order": "newest_first",
            "per_page": 20,
            "page": 1,
        }
        for key in ("price_from", "price_to"):
            if key in s:
                params[key] = s[key]
        params.update(s.get("extra", {}))

        items = client.search(params)
        first_time = s["name"] not in initialized  # 1er passage : on mémorise sans alerter
        new = 0

        for item in items:
            if item.get("id") is None:
                continue
            item_id = int(item["id"])
            if item_id in seen:
                continue
            seen.add(item_id)
            if first_time or is_excluded(item.get("title"), s.get("exclude", [])):
                continue
            notify(item, s["name"])
            new += 1
            time.sleep(1.1)  # on reste sous la limite d'envoi de Telegram

        initialized.add(s["name"])
        log.info(
            "%s : %d résultats, %d nouveau(x)%s",
            s["name"], len(items), new, " (initialisation)" if first_time else "",
        )


def main():
    if "COLLE_TON" in TELEGRAM_TOKEN + TELEGRAM_CHAT_ID:
        raise SystemExit("Renseigne TELEGRAM_TOKEN et TELEGRAM_CHAT_ID en haut du fichier.")

    seen, initialized = load_state()
    client = None
    pause = FIRST_BLOCK_PAUSE

    log.info("Bot démarré : %d recherche(s)", len(SEARCHES))
    telegram("sendMessage", text="✅ Bot Vinted démarré")

    try:
        while True:
            try:
                if client is None:
                    client = VintedClient()
                run_cycle(client, seen, initialized)
                save_state(seen, initialized)
                pause = FIRST_BLOCK_PAUSE
            except Blocked as e:
                wait = pause + random.uniform(0, 60)
                log.warning("Vinted bloque (HTTP %s). Pause de %d min.", e, wait // 60)
                client = None  # nouvelle session après la pause
                time.sleep(wait)
                pause = min(pause * 2, MAX_BLOCK_PAUSE)
                continue
            except Exception as e:
                log.warning("Erreur : %s", e)
            time.sleep(random.uniform(*CYCLE_DELAY))
    except KeyboardInterrupt:
        save_state(seen, initialized)
        log.info("Arrêt du bot.")


if __name__ == "__main__":
    main()
