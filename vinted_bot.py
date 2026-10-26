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
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7446117467")

DOMAIN = "www.vinted.fr"  # www.vinted.be, www.vinted.es, ...

SEARCHES = [
    {
        "name": "Nike Tech Fleece",
        "search_text": "nike tech fleece",
        "price_to": 15,
        "min_discount": 6,  # alerte seulement si -30 % ou plus sous le prix du marché
        # "chat_id": "987654321",  # optionnel : envoyer cette recherche à d'autres personnes
        "exclude": ["enfant", "lot", "défaut"],  # mots interdits dans le titre
    },
    {
        "name": "Switch OLED",
        "search_text": "nintendo switch oled",
        "price_from": 80,
        "price_to": 150,
        "min_discount": 20,
        "exclude": ["pour pièces", "défectueux", "boîte vide"],
        # "extra": {"attribute_ids[brand]": "53"},  # filtre Vinted optionnel (53 = Nike)
    },
]

CYCLE_DELAY = (45, 90)    # pause entre deux tours complets (secondes)
SEARCH_DELAY = (5, 12)    # pause entre deux recherches d'un même tour
FIRST_BLOCK_PAUSE = 300   # pause initiale si Vinted bloque (5 min), doublée à chaque fois
MAX_BLOCK_PAUSE = 3600    # ...jusqu'à 1 h maximum

# Détection des bonnes affaires : le bot compare chaque nouvelle annonce au prix médian
# des annonces récentes de la même recherche (sans plafond de prix, "price_from" conservé).
MIN_DISCOUNT = 25         # % minimum sous le prix du marché pour être alerté (0 = tout alerter)
                          # réglable par recherche avec la clé "min_discount"
BASELINE_PAGES = 3        # pages de 20 annonces utilisées pour estimer le prix du marché
BASELINE_REFRESH = 6 * 3600  # le prix du marché est recalculé toutes les 6 h
MIN_SAMPLE = 10           # en dessous, trop peu d'annonces pour comparer

# ─────────────────────────────────────────────────────────────────────────

STATE_FILE = Path(__file__).with_name("seen_items.json")
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/141.0.0.0 Safari/537.36"
)
ACCEPT_LANGUAGE = "fr-FR,fr;q=0.9,en;q=0.8"
API_HOST = "api." + DOMAIN.replace("www.", "", 1)  # api.vinted.fr
TOKEN_MAX_AGE = 6 * 3600  # on renouvelle le token Vinted toutes les 6 h

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("vinted-bot")


# ───────────────────────────── VINTED ─────────────────────────────

class Blocked(Exception):
    """Vinted refuse les requêtes (403 / 429)."""


class VintedClient:
    def __init__(self):
        self.session = None
        self.token = None
        self.anon_id = None
        self.token_time = 0.0
        self.refresh()

    def _cookie(self, name):
        """Dernière valeur non vide d'un cookie (Vinted en envoie d'abord une vide)."""
        jar = getattr(self.session.cookies, "jar", self.session.cookies)
        values = [c.value for c in jar if c.name == name and c.value]
        return values[-1] if values else None

    def refresh(self):
        """Nouvelle session + token anonyme (cookie access_token_web), comme le site."""
        if cffi_requests:
            self.session = cffi_requests.Session(impersonate="chrome")
        else:
            self.session = requests.Session()
            self.session.headers["User-Agent"] = USER_AGENT
        url = f"https://{DOMAIN}/catalog"
        headers = {"Accept-Language": ACCEPT_LANGUAGE}
        r = self.session.head(url, headers=headers, timeout=20, allow_redirects=True)
        if r.status_code in (403, 429):
            raise Blocked(r.status_code)
        self.token = self._cookie("access_token_web")
        if not self.token:  # plan B : la vraie page (lourde) donne aussi le cookie
            r = self.session.get(url, headers=headers, timeout=60)
            if r.status_code in (403, 429):
                raise Blocked(r.status_code)
            self.token = self._cookie("access_token_web")
        if not self.token:
            raise RuntimeError("token Vinted introuvable (cookie access_token_web)")
        self.anon_id = self._cookie("anon_id")
        self.token_time = time.time()
        log.info("Session Vinted initialisée (token reçu)")

    def search(self, params):
        if time.time() - self.token_time > TOKEN_MAX_AGE:
            self.refresh()
        url = f"https://{API_HOST}/svc-catalogue/items"
        for attempt in (1, 2):
            headers = {
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": ACCEPT_LANGUAGE,
                "Authorization": f"Bearer {self.token}",
                "Origin": f"https://{DOMAIN}",
                "Referer": f"https://{DOMAIN}/",
            }
            if self.anon_id:
                headers["x-anon-id"] = self.anon_id
            r = self.session.get(url, params=params, headers=headers, timeout=20)
            if r.status_code in (401, 403) and attempt == 1:
                self.refresh()  # token expiré ou refusé : on en demande un neuf
                continue
            break
        if r.status_code in (401, 403, 429):
            raise Blocked(r.status_code)
        if r.status_code == 404:
            raise RuntimeError("HTTP 404 : Vinted a encore changé son API, le bot est à mettre à jour")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} : {r.text[:200]}")
        data = r.json()
        if "items" not in data:
            log.warning("Réponse inattendue de Vinted (clés : %s)", ", ".join(data.keys()))
        return data.get("items") or []


# ───────────────────────────── OUTILS ─────────────────────────────

def parse_price(item):
    """Retourne (montant, devise), ou (None, devise) si le prix est introuvable."""
    for key in ("price", "total_item_price"):
        p = item.get(key)
        if isinstance(p, dict):
            amount, cur = p.get("amount"), p.get("currency_code", "EUR")
        else:
            amount, cur = p, item.get("currency", "EUR")
        try:
            return float(amount), cur
        except (TypeError, ValueError):
            continue
    return None, "EUR"


def is_excluded(title, words):
    t = (title or "").lower()
    return any(re.search(rf"\b{re.escape(w.lower())}\b", t) for w in words)


def item_url(item):
    url = item.get("url") or ""
    if url.startswith("/"):  # l'API renvoie maintenant des liens relatifs
        return f"https://{DOMAIN}{url}"
    return url or f"https://{DOMAIN}/items/{item['id']}"


def build_caption(item, search_name, deal=None):
    title = html.escape(item.get("title") or "Sans titre")
    amount, cur = parse_price(item)
    symbol = "€" if cur == "EUR" else cur
    price = f"{amount:.2f} {symbol}" if amount is not None else "prix : voir l'annonce"

    box = item.get("item_box") if isinstance(item.get("item_box"), dict) else {}
    details = [
        item.get("brand_title") or box.get("first_line"),  # marque
        box.get("second_line")  # "taille · état"
        or " · ".join(str(d) for d in (item.get("size_title"), item.get("status")) if d),
    ]
    details = [html.escape(str(d)) for d in details if d]

    lines = [f"🆕 <b>{title}</b>", f"💶 <b>{price}</b>"]
    if deal and deal[0] > 0:  # deal = (remise %, prix médian, nb d'annonces comparées)
        discount, market, n = deal
        flame = "🔥🔥" if discount >= 50 else "🔥" if discount >= 30 else "👍"
        lines.append(f"{flame} <b>-{discount} %</b> sous le marché (~{market:.0f} {symbol}, {n} annonces)")
        if discount >= 75:
            lines.append("⚠️ Prix très bas : vérifie l'annonce (arnaque, défaut, accessoire ?)")
    if details:
        lines.append(" · ".join(details))
    lines.append(f"🔎 {html.escape(search_name)}")
    lines.append(f'<a href="{html.escape(item_url(item))}">Voir sur Vinted</a>')
    return "\n".join(lines)


# ───────────────────────────── TELEGRAM ─────────────────────────────

def parse_chat_ids(value):
    """Accepte "123", "123,456" ou une liste ; retourne la liste des IDs."""
    if isinstance(value, (list, tuple)):
        value = ",".join(str(v) for v in value)
    return [c.strip() for c in str(value).split(",") if c.strip()]


def telegram(method, chat_id, **payload):
    payload["chat_id"] = chat_id
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
        log.warning("Telegram a refusé l'envoi à %s (HTTP %s) : %s", chat_id, r.status_code, r.text[:150])
    return r.ok


def notify(item, search_name, deal=None, chats=None):
    caption = build_caption(item, search_name, deal)
    photo = (item.get("photo") or {}).get("url")
    for chat_id in chats or parse_chat_ids(TELEGRAM_CHAT_ID):
        sent = False
        if photo:
            sent = telegram("sendPhoto", chat_id, photo=photo, caption=caption, parse_mode="HTML")
        if not sent:
            telegram("sendMessage", chat_id, text=caption, parse_mode="HTML")


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

def median(values):
    v = sorted(values)
    n = len(v)
    return v[n // 2] if n % 2 else (v[n // 2 - 1] + v[n // 2]) / 2


def build_params(s, for_market=False):
    """Paramètres de recherche. for_market=True : sans plafond de prix (calcul du prix du marché)."""
    params = {
        "search_text": s["search_text"],
        "order": "newest_first",
        "per_page": 20,
        "page": 1,
    }
    for key in (("price_from",) if for_market else ("price_from", "price_to")):
        if key in s:
            params[key] = s[key]
    params.update(s.get("extra", {}))
    return {k: v for k, v in params.items() if v not in (None, "")}  # filtres vides = erreur 400


def get_market_price(client, s, cache):
    """(prix médian, nb d'annonces) des annonces récentes de cette recherche. Mis en cache."""
    cached = cache.get(s["name"])
    if cached and time.time() - cached[2] < BASELINE_REFRESH:
        return cached[:2]
    prices = []
    for page in range(1, BASELINE_PAGES + 1):
        if page > 1:
            time.sleep(random.uniform(*SEARCH_DELAY))
        params = build_params(s, for_market=True)
        params["page"] = page
        for item in client.search(params):
            amount, _ = parse_price(item)
            if amount and not is_excluded(item.get("title"), s.get("exclude", [])):
                prices.append(amount)
    if len(prices) >= MIN_SAMPLE:
        result = (median(prices), len(prices))
        log.info("%s : prix du marché ~%.0f (médiane de %d annonces)", s["name"], result[0], result[1])
    else:
        result = (None, len(prices))
        log.info("%s : seulement %d annonces, pas de comparaison de prix", s["name"], len(prices))
    cache[s["name"]] = (*result, time.time())
    return result


def run_cycle(client, seen, initialized, market_cache=None):
    if market_cache is None:
        market_cache = {}
    for i, s in enumerate(SEARCHES):
        if i:
            time.sleep(random.uniform(*SEARCH_DELAY))

        items = client.search(build_params(s))
        first_time = s["name"] not in initialized  # 1er passage : on mémorise sans alerter
        min_disc = s.get("min_discount", MIN_DISCOUNT)
        chats = parse_chat_ids(s["chat_id"]) if s.get("chat_id") else None
        new = 0

        for item in items:
            if item.get("id") is None:
                continue
            item_id = int(item["id"])
            if item_id in seen:
                continue
            if first_time or is_excluded(item.get("title"), s.get("exclude", [])):
                seen.add(item_id)
                continue

            deal = None
            amount, _ = parse_price(item)
            if amount:
                market, n = get_market_price(client, s, market_cache)  # peut lever Blocked
                if market:
                    deal = (round((1 - amount / market) * 100), market, n)
            seen.add(item_id)  # après l'évaluation : si Vinted bloque, l'annonce sera revue
            if deal and min_disc > 0 and deal[0] < min_disc:
                continue  # pas assez en dessous du prix du marché
            notify(item, s["name"], deal, chats)
            new += 1
            time.sleep(1.1)  # on reste sous la limite d'envoi de Telegram

        initialized.add(s["name"])
        log.info(
            "%s : %d résultats, %d alerte(s)%s",
            s["name"], len(items), new, " (initialisation)" if first_time else "",
        )


def main():
    if "COLLE_TON" in TELEGRAM_TOKEN + TELEGRAM_CHAT_ID:
        raise SystemExit("Renseigne TELEGRAM_TOKEN et TELEGRAM_CHAT_ID en haut du fichier.")

    seen, initialized = load_state()
    client = None
    market_cache = {}
    pause = FIRST_BLOCK_PAUSE

    log.info("Bot démarré : %d recherche(s)", len(SEARCHES))
    telegram("sendMessage", parse_chat_ids(TELEGRAM_CHAT_ID)[0], text="✅ Bot Vinted démarré")

    try:
        while True:
            try:
                if client is None:
                    client = VintedClient()
                run_cycle(client, seen, initialized, market_cache)
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
