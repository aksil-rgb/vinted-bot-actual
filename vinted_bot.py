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
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "7446117467, -1004446820442 ")

DOMAIN = "www.vinted.fr"  # www.vinted.be, www.vinted.es, ...

SEARCHES = [
    {
        "name": "Nike",
        "search_text": "nike",
        "price_from": 1,
        "price_to": 50,
        "market_deals": False,  # pas de comparaison au prix du marché (recherche trop large)
        # Bon plan : un article dont le titre contient un de ces mots, à BARGAIN_MAX € ou moins
        "bargain_keywords": ["phenom", "aeroswift", "tech fleece", "run division", "windrunner"],
        "exclude": ["enfant", "enfants", "kids", "junior"],  # titres ignorés
        # "chat_id": "987654321",  # optionnel : envoyer cette recherche à d'autres personnes
        # "extra": {"attribute_ids[brand]": "53"},  # filtre Vinted optionnel (53 = Nike)
    },
    {
        "name": "Under Armour",
        "search_text": "under armour",
        "price_from": 1,
        "price_to": 50,
        "market_deals": False,
        "bargain_keywords": ["project rock", "coldgear", "unstoppable", "rival fleece", "curry"],
        "exclude": ["enfant", "enfants", "kids", "junior"],
    },
]

CYCLE_DELAY = (15, 30)    # pause entre deux tours complets (secondes) ; si Vinted bloque souvent, augmente-la
SEARCH_DELAY = (1, 3)     # pause entre deux recherches d'un même tour
FIRST_BLOCK_PAUSE = 60    # pause initiale si Vinted bloque (1 min), doublée à chaque fois
MAX_BLOCK_PAUSE = 600     # ...jusqu'à 10 min maximum
PER_PAGE = 20             # annonces lues par recherche et par tour (plus c'est bas, plus c'est rapide)

# Détection des bonnes affaires : le bot compare chaque nouvelle annonce au prix médian
# des annonces récentes de la même recherche (sans plafond de prix, "price_from" conservé).
MIN_DISCOUNT = 25         # % minimum sous le prix du marché pour être une "bonne offre"
                          # réglable par recherche avec la clé "min_discount"
ALERT_ALL_NEW = True      # True : toutes les nouvelles annonces + les bonnes offres mises en avant
                          # False : seulement les bonnes offres (réglable par recherche : "all_new")
SILENT_NEW = True         # les annonces normales arrivent sans son, les alertes importantes sonnent
BARGAIN_MAX = 10          # "bon plan" : prix maximum (€) pour les articles de "bargain_keywords"
                          # réglable par recherche avec la clé "bargain_max"
ENRICH_SELLER = True      # affiche la note (étoiles) et le nombre d'avis du vendeur
SELLER_STATS_ALL = True   # True : sur toutes les alertes ; False : seulement BON PLAN / BONNE OFFRE
SELLER_LOOKUPS_PER_CYCLE = 10  # nombre maximum de vendeurs interrogés par tour (limite les requêtes)
BUYER_PROTECTION = (0.70, 0.05)  # protection acheteur estimée : 0,70 € + 5 % (si Vinted ne donne pas le montant)
SHIPPING_ESTIMATE = 3.89  # livraison la moins chère estimée (point relais, petit colis) ; None = pas de livraison
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
        self.seller_cache = {}
        self.seller_failures = 0
        self.seller_disabled_until = 0.0
        self.lookups_this_cycle = 0
        self.max_per_page = None
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

    def _headers(self):
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": ACCEPT_LANGUAGE,
            "Authorization": f"Bearer {self.token}",
            "Origin": f"https://{DOMAIN}",
            "Referer": f"https://{DOMAIN}/",
        }
        if self.anon_id:
            headers["x-anon-id"] = self.anon_id
        return headers

    def search(self, params):
        if time.time() - self.token_time > TOKEN_MAX_AGE:
            self.refresh()
        if self.max_per_page and params.get("per_page", 0) > self.max_per_page:
            params = {**params, "per_page": self.max_per_page}
        url = f"https://{API_HOST}/svc-catalogue/items"
        for attempt in (1, 2):
            try:
                r = self.session.get(url, params=params, headers=self._headers(), timeout=15)
            except Exception as e:  # délai dépassé, coupure réseau : on réessaie tout de suite
                if attempt == 2:
                    raise
                log.info("Vinted n’a pas répondu à temps (%s) : nouvel essai", type(e).__name__)
                continue
            if r.status_code in (401, 403) and attempt == 1:
                self.refresh()  # token expiré ou refusé : on en demande un neuf
                continue
            break
        if r.status_code in (401, 403, 429):
            raise Blocked(r.status_code)
        if r.status_code == 400 and params.get("per_page", 0) > 20:
            log.warning("Vinted refuse %s résultats par page : retour à 20", params["per_page"])
            self.max_per_page = 20
            return self.search(params)
        if r.status_code == 404:
            raise RuntimeError("HTTP 404 : Vinted a encore changé son API, le bot est à mettre à jour")
        if r.status_code >= 400:
            raise RuntimeError(f"HTTP {r.status_code} : {r.text[:200]}")
        data = r.json()
        if "items" not in data:
            log.warning("Réponse inattendue de Vinted (clés : %s)", ", ".join(data.keys()))
        return data.get("items") or []

    def seller_stats(self, user_id):
        """(note sur 5, nombre d'avis) du vendeur, ou None. En cache, ne bloque jamais le bot."""
        if not ENRICH_SELLER or not user_id:
            return None
        if user_id in self.seller_cache:
            return self.seller_cache[user_id]
        if time.time() < self.seller_disabled_until or self.lookups_this_cycle >= SELLER_LOOKUPS_PER_CYCLE:
            return None
        self.lookups_this_cycle += 1
        time.sleep(random.uniform(0.2, 0.6))
        stats, answered = None, False
        try:
            r = self.session.get(
                f"https://{DOMAIN}/api/v2/users/{user_id}", headers=self._headers(), timeout=6
            )
            if r.status_code == 200:
                answered = True
                self.seller_failures = 0
                user = r.json().get("user") or {}
                count = user.get("feedback_count")
                note = user.get("feedback_rating")  # déjà sur 5
                if note is None and user.get("feedback_reputation") is not None:
                    reputation = float(user["feedback_reputation"])
                    note = reputation * 5 if reputation <= 1 else reputation
                if note is not None and count is not None:
                    stats = (float(note), int(count))
                else:
                    warn_once("no-feedback", "Réponse du vendeur sans note (champs reçus : %s)",
                              ", ".join(sorted(user.keys())) or "aucun")
            else:
                self.seller_failures += 1
                log.info("Avis du vendeur indisponibles (HTTP %s)", r.status_code)
        except Exception as e:
            self.seller_failures += 1
            log.info("Avis du vendeur indisponibles (%s)", type(e).__name__)
        if self.seller_failures >= 3:
            self.seller_failures = 0
            self.seller_disabled_until = time.time() + 1800
            log.warning("Avis des vendeurs en pause 30 min après 3 échecs : le bot continue sans")
        if answered:  # une vraie réponse est mise en cache ; un échec sera retenté plus tard
            self.seller_cache[user_id] = stats
        return stats


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


_warned = set()


def warn_once(key, message, *args):
    """Écrit un avertissement une seule fois par lancement du bot."""
    if key not in _warned:
        _warned.add(key)
        log.warning(message, *args)


def seller_of(item):
    """(id, pseudo, lien du profil) du vendeur, quand l'annonce les contient."""
    user = item.get("user") if isinstance(item.get("user"), dict) else {}
    uid, login = user.get("id") or item.get("user_id"), user.get("login")
    url = user.get("profile_url") or (f"https://{DOMAIN}/member/{uid}" if uid else None)
    if url and url.startswith("/"):
        url = f"https://{DOMAIN}{url}"
    return uid, login, url


def parse_total(item):
    """Prix avec protection acheteur (total_item_price), ou None."""
    p = item.get("total_item_price")
    if isinstance(p, dict):
        p = p.get("amount")
    try:
        return float(p)
    except (TypeError, ValueError):
        return None


def published_text(item):
    """Date de publication si l'annonce en porte une, sinon l'heure à laquelle le bot l'a vue."""
    hi = (item.get("photo") or {}).get("high_resolution") or {}
    ts = item.get("created_at_ts") or hi.get("timestamp")
    try:
        ts = float(ts)
        if ts > 1e11:  # millisecondes
            ts /= 1000
        age = max(0, int(time.time() - ts))
    except (TypeError, ValueError):
        return "Détecté à " + time.strftime("%H:%M")
    if age < 3600:
        return f"Publié il y a {max(1, age // 60)} min"
    if age < 86400:
        return f"Publié il y a {age // 3600} h"
    return f"Publié il y a {age // 86400} j"


def parse_amount(value):
    if isinstance(value, dict):
        value = value.get("amount")
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def cost_breakdown(item, amount):
    """(protection acheteur, livraison, total tout compris, protection estimée ?) ou None sans prix."""
    if amount is None:
        return None
    fee = parse_amount(item.get("service_fee"))
    total = parse_total(item)
    if fee is None and total is not None and total > amount:
        fee = total - amount  # Vinted donne le prix avec protection : on en déduit la protection
    estimated = fee is None
    if estimated:
        fee = BUYER_PROTECTION[0] + BUYER_PROTECTION[1] * amount
    shipping = SHIPPING_ESTIMATE or 0.0
    return fee, shipping, amount + fee + shipping, estimated


def fmt_rating(note, count):
    """Avis du vendeur en étoiles : ★★★★☆ 4,4 (437 avis)."""
    if not count:
        return "aucun avis"
    full = max(0, min(5, int(note + 0.5)))
    return "★" * full + "☆" * (5 - full) + f" {note:.1f}".replace(".", ",") + f" ({count} avis)"


def matches_any(title, words):
    """Vrai si le titre contient un des mots (début de mot : "phenom" trouve "phenome")."""
    t = (title or "").lower()
    return any(re.search(rf"\b{re.escape(w.lower())}", t) for w in words)


def fmt_price(amount, symbol):
    text = f"{amount:.2f}"
    if text.endswith(".00"):
        text = text[:-3]
    return f"{text.replace('.', ',')} {symbol}"


BANNER_BAR = "━" * 18


def build_caption(item, search_name, deal=None, label="Nouveau", seller=None, with_link=True):
    important = label != "Nouveau"
    title = html.escape(item.get("title") or "Sans titre")
    amount, cur = parse_price(item)
    symbol = "€" if cur == "EUR" else cur
    price = fmt_price(amount, symbol) if amount is not None else "prix : voir l’annonce"

    box = item.get("item_box") if isinstance(item.get("item_box"), dict) else {}
    details = [
        item.get("brand_title") or box.get("first_line"),  # marque
        box.get("second_line")  # "taille · état"
        or " · ".join(str(d) for d in (item.get("size_title"), item.get("status")) if d),
    ]
    details = [html.escape(str(d)) for d in details if d]

    name = html.escape(search_name)
    if important:  # bandeau très visible
        lines = [BANNER_BAR, f"🔥 <b>{html.escape(label)}</b> · {name}", BANNER_BAR]
    else:
        lines = [f"<b>{html.escape(label)}</b> · {name}"]
    lines += [title, " · ".join([f"<b>{price}</b>"] + details)]

    cost = cost_breakdown(item, amount)
    if cost:
        fee, shipping, total_all, _ = cost
        parts = [price, f"{fmt_price(fee, symbol)} protection"]
        if shipping:
            parts.append(f"{fmt_price(shipping, symbol)} livraison mini")
        lines.append(f"Total ≈ <b>{fmt_price(total_all, symbol)}</b> " + ("tout compris" if shipping else "hors livraison"))
        lines.append("(" + " + ".join(parts) + ")")

    if deal and deal[0] > 0 and important:  # deal = (remise %, prix médian, nb d'annonces)
        discount, market, _ = deal
        lines.append(f"<b>-{discount} % sous le marché</b> (~{market:.0f} {symbol})")
        if discount >= 75:
            lines.append("Attention : prix très bas, vérifie l’annonce (arnaque, défaut, accessoire ?)")

    _, login, _ = seller_of(item)
    if login or seller:
        parts = ([html.escape(str(login))] if login else []) + ([fmt_rating(*seller)] if seller else [])
        lines.append("Vendeur : " + " · ".join(parts))
    if important and seller and (seller[1] < 5 or seller[0] < 4.0):
        lines.append("Prudence : vendeur avec peu d’avis ou noté moins de 4/5")
    lines.append(published_text(item))
    if with_link:
        lines.append(f'<a href="{html.escape(item_url(item))}">Ouvrir l’annonce</a>')
    return "\n".join(lines)


# ───────────────────────────── TELEGRAM ─────────────────────────────

def parse_chat_ids(value):
    """Accepte "123", "123,456" ou une liste ; retourne la liste des IDs."""
    if isinstance(value, (list, tuple)):
        value = ",".join(str(v) for v in value)
    return [c for c in re.split(r"[,;\s]+", str(value)) if c]


def telegram(method, chat_id, **payload):
    payload["chat_id"] = chat_id
    for attempt in (1, 2, 3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
                json=payload,
                timeout=20,
            )
        except requests.RequestException as e:
            log.warning("Telegram injoignable (%s)", type(e).__name__)
            return False
        if r.status_code == 429 and attempt < 3:  # trop de messages d'un coup : Telegram dit combien attendre
            try:
                wait = int(r.json().get("parameters", {}).get("retry_after", 5))
            except (ValueError, TypeError, AttributeError):
                wait = 5
            log.info("Telegram demande d’attendre %s s (limite de messages) : alerte renvoyée ensuite", wait)
            time.sleep(min(wait, 60) + 0.5)
            continue
        break
    if not r.ok:
        log.warning("Telegram a refusé l'envoi à %s (HTTP %s) : %s", chat_id, r.status_code, r.text[:150])
    return r.ok


def notify(item, search_name, deal=None, chats=None, label="Nouveau", silent=False, seller=None):
    caption = build_caption(item, search_name, deal, label, seller, with_link=False)
    plain = build_caption(item, search_name, deal, label, seller, with_link=True)
    buttons = [{"text": "Acheter", "url": item_url(item)}]
    profile = seller_of(item)[2]
    if profile:
        buttons.append({"text": "Vendeur", "url": profile})
    markup = {"inline_keyboard": [buttons]}
    photo = (item.get("photo") or {}).get("url")
    for chat_id in chats or parse_chat_ids(TELEGRAM_CHAT_ID):
        sent = False
        if photo:
            sent = telegram("sendPhoto", chat_id, photo=photo, caption=caption, parse_mode="HTML",
                            reply_markup=markup, disable_notification=silent)
        if not sent:
            sent = telegram("sendMessage", chat_id, text=caption, parse_mode="HTML",
                            reply_markup=markup, disable_notification=silent)
        if not sent:  # dernier recours : texte seul avec un lien
            telegram("sendMessage", chat_id, text=plain, parse_mode="HTML", disable_notification=silent)


# ───────────────────────────── ÉTAT (anti-doublons) ─────────────────────────────

def load_state():
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        seen, initialized = set(data.get("seen", [])), set(data.get("initialized", []))
        if data.get("per_page", 20) < PER_PAGE:  # pages plus grandes : on mémorise sans alerter
            initialized = set()
        return seen, initialized
    except (OSError, ValueError):
        return set(), set()


def save_state(seen, initialized):
    recent = sorted(seen)[-5000:]  # les ids Vinted augmentent : on garde les plus récents
    STATE_FILE.write_text(
        json.dumps({"seen": recent, "initialized": sorted(initialized), "per_page": PER_PAGE}),
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
        "per_page": PER_PAGE,
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
    client.lookups_this_cycle = 0
    for i, s in enumerate(SEARCHES):
        if i:
            time.sleep(random.uniform(*SEARCH_DELAY))

        items = client.search(build_params(s))
        first_time = s["name"] not in initialized  # 1er passage : on mémorise sans alerter
        min_disc = s.get("min_discount", MIN_DISCOUNT)
        market_deals = s.get("market_deals", True)
        all_new = s.get("all_new", ALERT_ALL_NEW)
        bargain_max = s.get("bargain_max", BARGAIN_MAX)
        bargain_words = s.get("bargain_keywords", [])
        chats = parse_chat_ids(s["chat_id"]) if s.get("chat_id") else None
        new = important = 0

        for item in items:
            if item.get("id") is None:
                continue
            item_id = int(item["id"])
            if item_id in seen:
                continue
            if first_time or is_excluded(item.get("title"), s.get("exclude", [])):
                seen.add(item_id)
                continue

            amount, _ = parse_price(item)
            is_bargain = bool(
                bargain_words
                and amount is not None
                and amount <= bargain_max
                and matches_any(item.get("title"), bargain_words)
            )
            deal = None
            if market_deals and amount:
                market, n = get_market_price(client, s, market_cache)  # peut lever Blocked
                if market:
                    deal = (round((1 - amount / market) * 100), market, n)
            seen.add(item_id)  # après l'évaluation : si Vinted bloque, l'annonce sera revue

            is_deal = bool(deal) and deal[0] > 0 and deal[0] >= min_disc
            judged = deal is not None or not market_deals  # a-t-on pu juger cette annonce ?
            if not (is_bargain or is_deal) and not all_new and judged:
                continue  # ni bon plan, ni bonne offre, et les nouveautés ne sont pas demandées

            label = "BON PLAN" if is_bargain else "BONNE OFFRE" if is_deal else "Nouveau"
            stats = None
            if SELLER_STATS_ALL or label != "Nouveau":
                uid = seller_of(item)[0]
                if uid is None:
                    warn_once("no-seller", "Les annonces ne contiennent pas le vendeur (champs reçus : %s) : "
                              "pas d’avis possibles", ", ".join(sorted(item.keys())))
                stats = client.seller_stats(uid)
            notify(item, s["name"], deal, chats, label=label,
                   silent=SILENT_NEW and label == "Nouveau", seller=stats)
            new += 1
            important += label != "Nouveau"
            time.sleep(1.1)  # on reste sous la limite d'envoi de Telegram

        initialized.add(s["name"])
        log.info(
            "%s : %d résultats, %d alerte(s) dont %d importante(s)%s",
            s["name"], len(items), new, important, " (initialisation)" if first_time else "",
        )


def debug_vinted():
    """python vinted_bot.py --debug : montre ce que Vinted renvoie (jamais ton token) pour diagnostiquer."""
    client = VintedClient()
    s = SEARCHES[0]
    items = client.search(build_params(s))
    print(f"\n{len(items)} annonces reçues pour « {s['name']} »")
    if not items:
        return
    item = items[0]
    print("Champs d'une annonce :", ", ".join(sorted(item.keys())))
    for key in ("title", "price", "total_item_price", "service_fee", "user", "item_box", "url"):
        print(f"  {key} = {str(item.get(key))[:160]}")
    uid = seller_of(item)[0]
    print("\nIdentifiant vendeur :", uid)
    if not uid:
        return
    r = client.session.get(f"https://{DOMAIN}/api/v2/users/{uid}", headers=client._headers(), timeout=15)
    print("Profil vendeur : HTTP", r.status_code)
    if r.status_code != 200:
        print(r.text[:200])
        return
    user = r.json().get("user") or {}
    print("Champs du profil :", ", ".join(sorted(user.keys())))
    for key in sorted(user):
        if "feedback" in key or "rating" in key or "reputation" in key:
            print(f"  {key} = {user[key]}")


def describe_recipients():
    """Lignes qui disent à qui le bot enverra les alertes (pour repérer une mauvaise configuration)."""
    ids = parse_chat_ids(TELEGRAM_CHAT_ID)
    note = ""
    if os.getenv("TELEGRAM_CHAT_ID"):
        note = " [lus depuis la variable d’environnement TELEGRAM_CHAT_ID, PAS depuis la ligne 44 du fichier]"
    lines = [f"Alertes envoyées à {len(ids)} destinataire(s) : {', '.join(ids)}{note}"]
    if not any(i.startswith(("-", "@")) for i in ids):
        lines.append("Attention : aucun canal ni groupe dans la liste (leur identifiant commence par - ou @)")
    for s in SEARCHES:
        if s.get("chat_id"):
            lines.append(f"Recherche « {s['name']} » : envoyée uniquement à {', '.join(parse_chat_ids(s['chat_id']))}")
    return lines


def tg_api(method, **params):
    """Appel simple de l'API Telegram pour les commandes de diagnostic : (réponse, erreur)."""
    try:
        r = requests.get(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}", params=params, timeout=20)
        return r.json(), None
    except requests.RequestException as e:
        return None, f"Telegram injoignable ({type(e).__name__})"
    except ValueError:
        return None, "Réponse inattendue de Telegram"


def show_chat_ids(handle=None):
    """python vinted_bot.py --ids [@canal] : trouve l'identifiant d'un canal et vérifie que le bot peut y publier."""
    if "COLLE_TON" in TELEGRAM_TOKEN:
        print("Mets d'abord ton token Telegram à la ligne 43 de vinted_bot.py.")
        return
    me, err = tg_api("getMe")
    if err or not me.get("ok"):
        print(err or f"Telegram refuse ce token ({me.get('description')}) : vérifie la ligne 43 de vinted_bot.py.")
        return
    bot = me["result"]
    print(f"Bot : @{bot.get('username')}\n")

    if handle:  # canal public indiqué par son @nom : pas besoin d'attendre un message
        info, err = tg_api("getChat", chat_id=handle)
        if err or not info.get("ok"):
            print(err or f"Canal introuvable ({info.get('description')}). Le canal est-il public, et le @nom exact ?")
            return
        chat = info["result"]
        print(f"Canal « {chat.get('title')} »  ->  {chat['id']}")
        member, err = tg_api("getChatMember", chat_id=chat["id"], user_id=bot["id"])
        if err or not member.get("ok"):
            print("Le bot n'est PAS dans ce canal : ajoute-le comme administrateur.")
        else:
            status = member["result"].get("status")
            if status == "administrator" and member["result"].get("can_post_messages", True):
                print("Le bot est administrateur et peut publier : c'est bon.")
            elif status == "administrator":
                print("Le bot est administrateur mais sans le droit « Publier des messages » : active-le.")
            else:
                print(f"Le bot est « {status} » : il doit être ADMINISTRATEUR du canal.")
        print("\nMets ce nombre à la ligne 44, après ton ID perso et une virgule.")
        return

    data, err = tg_api("getUpdates")
    if err:
        print(err)
        return
    if not data.get("ok"):
        print(f"Telegram répond : {data.get('description')}")
        if data.get("error_code") == 409:
            print("Un autre programme (ou un webhook) utilise déjà ce bot et reçoit ses messages à sa place.")
        return
    chats = {}
    for update in data.get("result", []):
        for key in ("my_chat_member", "channel_post", "edited_channel_post", "message"):
            chat = (update.get(key) or {}).get("chat")
            if chat:
                name = chat.get("title") or chat.get("username") or chat.get("first_name") or "?"
                chats[chat["id"]] = (chat.get("type", "?"), name)
    if not chats:
        print("Aucun canal ni discussion trouvé (Telegram ne garde ces informations que 24 h).\n"
              "Plus simple : rends le canal public quelques minutes, puis lance\n"
              "  python vinted_bot.py --ids @nom_du_canal")
        return
    print("Identifiants à copier à la ligne 44 (le nombre, avec le signe - s'il y en a un) :\n")
    for chat_id, (kind, name) in chats.items():
        print(f"  {kind:<10} {name}  ->  {chat_id}")


def test_telegram():
    """python vinted_bot.py --test : envoie un message de test à chaque ID de la ligne 44."""
    if "COLLE_TON" in TELEGRAM_TOKEN + TELEGRAM_CHAT_ID:
        print("Renseigne d'abord le token (ligne 43) et le chat ID (ligne 44).")
        return
    for line in describe_recipients():
        print(line)
    print()
    for chat_id in parse_chat_ids(TELEGRAM_CHAT_ID):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id": chat_id, "text": "Test du bot Vinted : ça marche."},
                timeout=20,
            )
            ok = r.ok
            detail = "" if ok else (r.json().get("description") or r.text[:100])
        except (requests.RequestException, ValueError) as e:
            ok, detail = False, type(e).__name__
        print(f"  {chat_id} : " + ("OK, message envoyé" if ok else f"ÉCHEC ({detail})"))


def main():
    import sys
    if "--debug" in sys.argv:
        debug_vinted()
        return
    if "--ids" in sys.argv:
        rest = [a for a in sys.argv[sys.argv.index("--ids") + 1:] if not a.startswith("--")]
        show_chat_ids(rest[0] if rest else None)
        return
    if "--test" in sys.argv:
        test_telegram()
        return
    if "COLLE_TON" in TELEGRAM_TOKEN + TELEGRAM_CHAT_ID:
        raise SystemExit("Renseigne TELEGRAM_TOKEN et TELEGRAM_CHAT_ID en haut du fichier.")

    seen, initialized = load_state()
    client = None
    market_cache = {}
    pause = FIRST_BLOCK_PAUSE

    log.info("Bot démarré : %d recherche(s)", len(SEARCHES))
    for line in describe_recipients():
        log.info(line)
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
