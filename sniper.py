#!/usr/bin/env python3
"""
Zoekertjes-sniper: houdt 2dehands.be, Marktplaats.nl, Vinted.be en
Kleinanzeigen.de in de gaten en stuurt nieuwe zoekertjes naar Telegram.

Bediening via Telegram:
  iphone 16                        -> knoppen: platform, afstand, prijs kiezen
  iphone 16 tot 500                -> meteen toevoegen (snelle schrijfwijze)
  iphone 16 vanaf 200 tot 500 binnen 25 km op 2dehands en vinted
  lijst                            -> toont je zoekopdrachten
  stop 2                           -> verwijdert zoekopdracht nr. 2
  help

Extra:
  - berichten van de bot worden na 24 uur automatisch verwijderd
  - scamfilter: verdachte woorden eruit, verdacht lage prijzen gemarkeerd

Draaien:
  python3 sniper.py               -> één keer checken
  python3 sniper.py --duur 14     -> 14 minuten blijven draaien (GitHub Actions)
  python3 sniper.py --loop 120    -> blijft checken, elke 120 seconden (eigen computer)

Nodig: omgevingsvariabelen TELEGRAM_TOKEN en TELEGRAM_CHAT_ID
(optioneel POSTCODE en LOCATIE="lat,lon" voor afstanden).
Gebruikt alleen de standaardbibliotheek van Python, niets te installeren.
"""
import argparse
import html
import json
import math
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.path.join(BASE, "state.json")

SITES = {
    "2dehands": "https://www.2dehands.be",
    "marktplaats": "https://www.marktplaats.nl",
    "vinted": "https://www.vinted.be",
    "kleinanzeigen": "https://www.kleinanzeigen.de",
}
SITE_NAMES = {"2dehands": "2dehands", "marktplaats": "Marktplaats",
              "vinted": "Vinted", "kleinanzeigen": "Kleinanzeigen"}
KM_SITES = ("2dehands", "marktplaats")   # sites waar afstand werkt
SITE_RE = "2dehands|marktplaats|vinted|kleinanzeigen"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
MAX_SEEN_PER_SEARCH = 600
MAX_ALERTS_PER_RUN = 15          # voorkomt een spervuur aan berichten
ERROR_NOTIFY_EVERY = 6 * 3600    # foutmelding hoogstens om de 6 uur
DELETE_AFTER = 24 * 3600         # berichten van de bot verwijderen na 24 uur

# Scamfilter: zoekertjes met deze woorden in titel of beschrijving worden overgeslagen
SCAM_WORDS = [
    "whatsapp", "whats app", "wa.me", "telegram", "signal app",
    "vooruitbetal", "vooraf betal", "voorschot", "western union", "moneygram",
    "vrienden en familie", "friends and family", "friends & family",
    "replica", "1:1", "fake", "nagemaakt", "namaak",
    "icloud lock", "icloud locked", "icloud vergrendeld", "activation lock",
    "geblokkeerd", "blacklist", "gestolen",
]
LOW_PRICE_RATIO = 0.4            # < 40% van de gewone prijs = verdacht

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
POSTCODE = os.environ.get("POSTCODE", "").strip()   # geheim, voor afstanden
try:  # geheim, "lat,lon" van thuis: afstand voor Marktplaats (kent geen BE-postcodes)
    HOME = tuple(float(x) for x in os.environ.get("LOCATIE", "").split(","))[:2] or None
except ValueError:
    HOME = None

SENT_LOG = []   # (message_id, tijd) van berichten die de bot stuurde


# ---------------------------------------------------------------- status

def load_state():
    state = {"searches": [], "seen": {}, "update_offset": 0, "next_id": 1,
             "last_error_notice": 0, "sent": [], "draft": None}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state.update(json.load(f))
    return state


def save_state(state):
    state["sent"].extend(SENT_LOG)
    SENT_LOG.clear()
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- http / telegram

def http_json(url, data=None, timeout=25):
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA,
        "Accept": "application/json",
        "Accept-Language": "nl-BE,nl;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def tg(method, **params):
    for k, v in list(params.items()):
        if isinstance(v, (dict, list)):
            params[k] = json.dumps(v)
    data = urllib.parse.urlencode(params).encode()
    wait = int(params.get("timeout", 0))
    return http_json(f"https://api.telegram.org/bot{TOKEN}/{method}", data, timeout=wait + 25)


def send(text, buttons=None, preview=True):
    """Stuurt een bericht; geeft het message_id terug."""
    params = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    if buttons:
        params["reply_markup"] = {"inline_keyboard": buttons}
    if not preview:
        params["link_preview_options"] = {"is_disabled": True}
    try:
        res = tg("sendMessage", **params)
        mid = res["result"]["message_id"]
        SENT_LOG.append([mid, time.time()])
        return mid
    except urllib.error.HTTPError as e:
        print(f"Telegram-fout: {e} {e.read()[:300]!r}", file=sys.stderr)
    except Exception as e:
        print(f"Telegram-fout: {e}", file=sys.stderr)
    return None


def edit(message_id, text, buttons=None):
    params = {"chat_id": CHAT_ID, "message_id": message_id, "text": text,
              "parse_mode": "HTML", "link_preview_options": {"is_disabled": True}}
    if buttons is not None:
        params["reply_markup"] = {"inline_keyboard": buttons}
    try:
        tg("editMessageText", **params)
    except Exception as e:  # bv. "message is not modified"
        print(f"Bewerken mislukt: {e}", file=sys.stderr)


def cleanup_messages(state):
    """Verwijdert berichten die ouder zijn dan DELETE_AFTER (Telegram kan tot 48 uur)."""
    now, keep = time.time(), []
    for mid, ts in state.get("sent", []):
        if now - ts < DELETE_AFTER:
            keep.append([mid, ts])
        elif now - ts < 47 * 3600:
            try:
                tg("deleteMessage", chat_id=CHAT_ID, message_id=mid)
            except Exception:
                pass  # al weg of te oud: niets aan te doen
    state["sent"] = keep


# ---------------------------------------------------------------- zoeken

PRICE_TYPES = {
    "FAST_BID": "Bieden", "MIN_BID": "Bieden", "BID": "Bieden",
    "FREE": "Gratis", "SEE_DESCRIPTION": "Zie omschrijving",
    "RESERVED": "Gereserveerd", "EXCHANGE": "Ruilen", "ON_REQUEST": "Op aanvraag",
    "VB": "Bieden (VB)", "Zu verschenken": "Gratis",
}

VINTED_RE = re.compile(
    r'\\"productItem\\":\{\\"id\\":(\d+),\\"title\\":\\"(.*?)\\",\\"url\\":\\"(/items/[^"\\\\]+)\\"'
    r'.*?\\"price\\":\{\\"amount\\":\\"([\d.]+)\\"', re.S)


def _unescape(t):
    for _ in range(2):  # tekst zit dubbel ge-escaped in de pagina
        if "\\" not in t:
            break
        try:
            t = json.loads('"' + t + '"')
        except ValueError:
            break
    return t


def search_vinted(query):
    """Vinted heeft geen open zoek-API meer: de resultaten staan in de pagina zelf."""
    params = {"search_text": query, "order": "newest_first"}
    url = SITES["vinted"] + "/catalog?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html", "Accept-Language": "nl-BE,nl;q=0.9"})
    with urllib.request.urlopen(req, timeout=40) as r:
        page = r.read().decode("utf-8", "replace")
    items, ids = [], set()
    for m in VINTED_RE.finditer(page):
        item_id, title, path, amount = m.groups()
        if item_id in ids:
            continue
        ids.add(item_id)
        items.append({
            "id": f"v{item_id}", "site": "vinted",
            "title": _unescape(title).strip(), "desc": "",
            "price": float(amount), "price_type": "",
            "city": "", "url": SITES["vinted"] + path, "dist": None,
        })
        if len(items) >= 40:
            break
    return items


def _clean(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def parse_kleinanzeigen(page):
    items = []
    for m in re.finditer(r'<article\b([^>]*\bdata-adid="(\d+)"[^>]*)>(.*?)</article>', page, re.S):
        attrs, ad_id, body = m.groups()
        href = re.search(r'data-href="([^"]+)"', attrs)
        title = re.search(r'<a[^>]*class="[^"]*ellipsis[^"]*"[^>]*>(.*?)</a>', body, re.S)
        price_m = re.search(r'price-shipping--price[^>]*>(.*?)</p>', body, re.S)
        loc = re.search(r'aditem-main--top--left[^>]*>(.*?)</div>', body, re.S)
        desc = re.search(r'aditem-main--middle--description[^>]*>(.*?)</p>', body, re.S)
        price_txt = _clean(price_m.group(1)) if price_m else ""
        num = re.search(r"(\d[\d.]*)\s*€", price_txt)
        items.append({
            "id": f"k{ad_id}", "site": "kleinanzeigen",
            "title": _clean(title.group(1)) if title else "",
            "desc": _clean(desc.group(1)) if desc else "",
            "price": float(num.group(1).replace(".", "")) if num else None,
            "price_type": "" if num else (price_txt or "SEE_DESCRIPTION"),
            "city": _clean(loc.group(1)) if loc else "",
            "url": SITES["kleinanzeigen"] + (href.group(1) if href else f"/s-anzeige/{ad_id}"),
            "dist": None,
        })
    return items


def search_kleinanzeigen(query):
    slug = urllib.parse.quote(re.sub(r"\s+", "-", query.strip().lower()))
    url = f"{SITES['kleinanzeigen']}/s-sortierung:neueste/{slug}/k0"
    req = urllib.request.Request(url, headers={
        "User-Agent": UA, "Accept": "text/html", "Accept-Language": "de-DE,de;q=0.9"})
    with urllib.request.urlopen(req, timeout=25) as r:
        return parse_kleinanzeigen(r.read().decode("utf-8", "replace"))


def search_site(site, query, km=None, postcode=None):
    if site == "vinted":
        return search_vinted(query)
    if site == "kleinanzeigen":
        return search_kleinanzeigen(query)
    params = {
        "query": query,
        "limit": 30,
        "offset": 0,
        "sortBy": "SORT_INDEX",       # nieuwste eerst
        "sortOrder": "DECREASING",
        "viewOptions": "list-view",
    }
    if km and postcode and site == "2dehands":
        params["postcode"] = postcode
        params["distanceMeters"] = int(km * 1000)
    elif km and site == "marktplaats":
        params["limit"] = 100  # landelijk zoeken, zelf filteren op afstand
    url = SITES[site] + "/lrp/api/search?" + urllib.parse.urlencode(params)
    data = http_json(url)
    items = []
    for l in data.get("listings", []):
        item_id = l.get("itemId")
        if not item_id:
            continue
        pi = l.get("priceInfo") or {}
        cents = pi.get("priceCents")
        loc = l.get("location") or {}
        dist = None
        if km and site == "2dehands" and (loc.get("distanceMeters") or -1) >= 0:
            dist = loc["distanceMeters"] / 1000
        elif km and site == "marktplaats" and HOME and loc.get("latitude"):
            dist = haversine(HOME, (loc["latitude"], loc["longitude"]))
        items.append({
            "id": item_id, "site": site,
            "title": (l.get("title") or "").strip(),
            "desc": l.get("description") or "",
            "price": cents / 100 if cents else None,
            "price_type": pi.get("priceType", ""),
            "city": loc.get("cityName", ""),
            "url": SITES[site] + l.get("vipUrl", ""),
            "dist": dist,
        })
    return items


def haversine(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + \
        math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371 * 2 * math.asin(math.sqrt(h))


# ---------------------------------------------------------------- filteren

def euro(p):
    return f"€ {p:,.0f}".replace(",", ".") if p == int(p) else f"€ {p:.2f}"


def price_text(item):
    if item["price"] is not None:
        return euro(item["price"])
    pt = item["price_type"]
    return PRICE_TYPES.get(pt, pt if pt and not pt.isupper() else "Prijs onbekend")


def title_ok(query, title):
    """Elk woord van de zoekopdracht moet in de titel staan ('15' past niet op '150' of '18')."""
    t = (title or "").lower()
    for word in query.lower().split():
        if not re.search(r"(?<![0-9])" + re.escape(word) + (r"(?![0-9])" if word[-1].isdigit() else ""), t):
            return False
    return True


def scam_word(item):
    text = f"{item['title']} {item.get('desc', '')}".lower()
    for w in SCAM_WORDS:
        if w in text:
            return w
    return None


def matches(search, item):
    if not title_ok(search["query"], item["title"]):
        return False
    t = item["title"].lower()
    if any(w in t for w in search.get("exclude") or []):
        return False
    p = item["price"]
    if search.get("max_price") is not None and p is not None and p > search["max_price"]:
        return False
    if search.get("min_price") is not None and (p is None or p < search["min_price"]):
        return False
    km = search.get("km")
    if km and item["site"] == "marktplaats":
        return item.get("dist") is not None and item["dist"] <= km
    return True


def typical_price(items):
    prices = [i["price"] for i in items if i["price"]]
    return statistics.median(prices) if len(prices) >= 5 else None


def format_item(i, s, normal=None):
    dist = f"{i['dist']:.0f} km" if i.get("dist") is not None else ""
    info = " · ".join(x for x in (price_text(i), html.escape(i["city"]), dist,
                                  SITE_NAMES.get(i["site"], i["site"])) if x)
    warn = ""
    if normal and i["price"] and i["price"] < normal * LOW_PRICE_RATIO:
        warn = f"\n⚠️ Opgelet: veel goedkoper dan normaal (± {euro(normal)}), mogelijk scam"
    return (f"🎯 <b>{html.escape(i['title'])}</b>\n"
            f"{info}{warn}\n"
            f"Zoekopdracht: {html.escape(s['query'])}\n"
            f"{html.escape(i['url'])}")


def check_searches(state):
    """Geeft (aantal meldingen, lijst fouten) terug."""
    sent, errors = 0, []
    for s in state["searches"]:
        key = str(s["id"])
        seen = state["seen"].setdefault(key, [])
        seen_set = set(seen)
        found = []
        km = s.get("km")
        pc = s.get("postcode") or POSTCODE
        if km and "2dehands" in s["sites"] and not pc:
            errors.append(f"'{s['query']}': geen POSTCODE ingesteld voor de afstand")
        if km and "marktplaats" in s["sites"] and not HOME:
            errors.append(f"'{s['query']}': geen LOCATIE ingesteld voor Marktplaats-afstand")
        for site in s["sites"]:
            try:
                found.extend(search_site(site, s["query"], km, pc))
            except Exception as e:  # netwerk, blokkering, gewijzigde site...
                errors.append(f"{site} / '{s['query']}': {e}")
                print(f"Fout bij {site} '{s['query']}': {e}", file=sys.stderr)

        new = []
        for i in found:  # zelfde zoekertje op 2 sites = 1 melding
            if i["id"] not in seen_set:
                seen_set.add(i["id"])
                seen.append(i["id"])
                new.append(i)

        good = [i for i in found if title_ok(s["query"], i["title"]) and not scam_word(i)]
        normal = typical_price(good)

        if not s.get("primed"):
            # eerste keer: bestaande zoekertjes onthouden en de 3 nieuwste tonen
            if found:
                s["primed"] = True
                preview = [i for i in good if matches(s, i)][:3]
                if preview:
                    send(f"👀 Zo ziet het eruit voor <b>{html.escape(s['query'])}</b> "
                         f"(nieuwste nu online, hierna krijg je alleen nieuwe):")
                else:
                    send(f"👀 Nu staat er niets online voor <b>{html.escape(s['query'])}</b> "
                         f"met jouw filters. Ik laat het weten zodra er iets bijkomt.")
                for i in preview:
                    send(format_item(i, s, normal))
            continue

        for i in new:
            if not matches(s, i) or sent >= MAX_ALERTS_PER_RUN:
                continue
            w = scam_word(i)
            if w:
                print(f"Scamfilter: '{i['title']}' overgeslagen ({w})")
                continue
            send(format_item(i, s, normal))
            sent += 1

        state["seen"][key] = seen[-MAX_SEEN_PER_SEARCH:]
    return sent, errors


# ---------------------------------------------------------------- tekst-commando's

HELP = (
    "<b>Zoekertjes-sniper</b>\n"
    "Stuur gewoon wat je zoekt, bv. <code>iphone 16</code>: ik vraag dan met knoppen "
    "welk platform, hoe ver en welke prijs.\n\n"
    "Sneller kan ook in één bericht:\n"
    "<code>iphone 16 tot 500</code>\n"
    "<code>iphone 16 vanaf 200 tot 500</code>\n"
    "<code>fiets binnen 15 km</code> (2dehands + Marktplaats)\n"
    "<code>ps5 op 2dehands en vinted</code>\n"
    "<code>iphone 15 -hoesje -zoek</code> (woorden uitsluiten)\n\n"
    "<code>lijst</code> — je zoekopdrachten\n"
    "<code>stop 2</code> — zoekopdracht 2 verwijderen\n\n"
    "Je krijgt alleen nieuwe zoekertjes. Verdachte zoekertjes (WhatsApp, vooruitbetalen, "
    "replica, iCloud-lock...) filter ik eruit, en een verdacht lage prijs krijgt een ⚠️. "
    "Mijn berichten verdwijnen na 24 uur."
)


def parse_zoek(arg):
    """Geeft (zoekterm, minprijs, maxprijs, sites, km, postcode, iets_opgegeven, uitsluiten) terug."""
    sites, min_p, max_p, km, pc = list(SITES), None, None, None, None
    explicit_sites = given = False
    words = arg.split()  # '-woord' = zoekertjes met dat woord overslaan
    excl = [w[1:].lower().strip(",") for w in words if w.startswith("-") and len(w) > 1]
    arg = " ".join(w for w in words if not (w.startswith("-") and len(w) > 1))
    num = r"€?\s*(\d+(?:[.,]\d+)?)\s*(?:euro|eur|€)?"
    for _ in range(4):  # 'tot', 'vanaf', 'op' en 'binnen' in om het even welke volgorde
        m = re.search(r"\s+binnen\s+(\d+(?:[.,]\d+)?)\s*km(?:\s+(?:van|rond)\s+(\w+))?\s*$", arg, re.I)
        if m:
            km = float(m.group(1).replace(",", "."))
            pc = m.group(2)
            arg, given = arg[:m.start()], True
        m = re.search(rf"\s+op\s+((?:{SITE_RE})(?:\s*(?:,|\+|en)\s*(?:{SITE_RE}))*)\s*$", arg, re.I)
        if m:
            sites = list(dict.fromkeys(re.findall(SITE_RE, m.group(1).lower())))
            arg, given, explicit_sites = arg[:m.start()], True, True
        m = re.search(rf"\s+(?:tot|max)\s+{num}\s*$", arg, re.I)
        if m:
            max_p = float(m.group(1).replace(",", "."))
            arg, given = arg[:m.start()], True
        m = re.search(rf"\s+(?:vanaf|min)\s+{num}\s*$", arg, re.I)
        if m:
            min_p = float(m.group(1).replace(",", "."))
            arg, given = arg[:m.start()], True
    if km and not explicit_sites:
        sites = list(KM_SITES)
    return arg.strip(), min_p, max_p, sites, km, pc, given, excl


def parse_price(text):
    """'100-300', 'tot 300', 'vanaf 100', '300' -> (min, max) of None."""
    t = text.lower().replace("€", "").replace("euro", "").strip()
    m = re.fullmatch(r"(\d+)\s*(?:-|tot|à|a)\s*(\d+)", t)
    if m:
        a, b = sorted((float(m.group(1)), float(m.group(2))))
        return a, b
    m = re.fullmatch(r"(?:tot|max|maximum)\s*(\d+)", t) or re.fullmatch(r"(\d+)", t)
    if m:
        return None, float(m.group(1))
    m = re.fullmatch(r"(?:vanaf|min|minimum)\s*(\d+)", t)
    if m:
        return float(m.group(1)), None
    return None


def describe(s):
    extra = ""
    if s.get("min_price") is not None and s.get("max_price") is not None:
        extra += f" {euro(s['min_price'])}–{euro(s['max_price'])}"
    elif s.get("max_price") is not None:
        extra += f" tot {euro(s['max_price'])}"
    elif s.get("min_price") is not None:
        extra += f" vanaf {euro(s['min_price'])}"
    if s.get("km"):
        extra += f" binnen {s['km']:g} km"
    if s.get("exclude"):
        extra += " zonder " + ", ".join(html.escape(w) for w in s["exclude"])
    sites = " + ".join(SITE_NAMES[x] for x in s["sites"])
    return f"{s['id']}. {html.escape(s['query'])}{extra} ({sites})"


def add_search(state, query, min_p, max_p, sites, km, pc=None, exclude=None):
    s = {"id": state["next_id"], "query": query, "min_price": min_p, "max_price": max_p,
         "sites": sites, "km": km, "postcode": pc, "exclude": exclude or [], "primed": False}
    state["next_id"] += 1
    state["searches"].append(s)
    return s


def run_command(state, text):
    if not text:
        return None
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower().lstrip("/")
    arg = parts[1].strip() if len(parts) > 1 else ""

    draft = state.get("draft")
    if draft and draft.get("step") == "price" and cmd not in ("lijst", "stop", "help", "start"):
        pr = parse_price(text)
        if pr:
            draft["min"], draft["max"] = pr
            return finish_draft(state)
        if text.lower() in ("geen", "geen limiet", "nee"):
            return finish_draft(state)

    if cmd in ("start", "help"):
        return HELP

    if cmd == "lijst":
        if not state["searches"]:
            return "Nog geen zoekopdrachten. Stuur bv. iphone 16"
        return "<b>Je zoekopdrachten</b>\n" + "\n".join(describe(s) for s in state["searches"])

    if cmd == "stop":
        if not arg.isdigit():
            return "Welke? Bv. stop 2 (nummers zie je met lijst)"
        n = int(arg)
        before = len(state["searches"])
        state["searches"] = [s for s in state["searches"] if s["id"] != n]
        state["seen"].pop(str(n), None)
        return f"🗑️ Zoekopdracht {n} verwijderd." if len(state["searches"]) < before \
            else f"Zoekopdracht {n} bestaat niet. Zie lijst."

    if cmd != "zoek":
        arg = text  # gewone tekst = nieuwe zoekopdracht
    query, min_p, max_p, sites, km, pc, given, excl = parse_zoek(arg)
    if not query:
        return "Wat moet ik zoeken? Bv. iphone 16"
    if given:  # snelle schrijfwijze: meteen toevoegen
        s = add_search(state, query, min_p, max_p, sites, km, pc, excl)
        return f"✅ Toegevoegd:\n{describe(s)}\nIk meld vanaf nu nieuwe zoekertjes."
    start_draft(state, query, excl)
    return None


# ---------------------------------------------------------------- knoppen (wizard)

def btn(text, data):
    return {"text": text, "callback_data": data}


def draft_title(d):
    return f"🔎 <b>{html.escape(d['query'])}</b>"


def show_sites(d):
    rows = [[btn(("✅ " if s in d["sites"] else "⬜ ") + SITE_NAMES[s], f"p:{s}")] for s in SITES]
    rows.append([btn("Verder ➡️", "next"), btn("✖️ Annuleer", "cancel")])
    return f"{draft_title(d)}\nStap 1/3 — Op welke platformen zoeken?", rows


def show_km(d):
    rows = [[btn("5 km", "km:5"), btn("10 km", "km:10"), btn("15 km", "km:15")],
            [btn("25 km", "km:25"), btn("50 km", "km:50"), btn("100 km", "km:100")],
            [btn("🌍 Maakt niet uit", "km:0")]]
    return (f"{draft_title(d)}\nStap 2/3 — Hoe ver mag het zijn? "
            f"(geldt voor 2dehands en Marktplaats)"), rows


def show_price(d):
    rows = [[btn("tot €50", "pr:0-50"), btn("tot €100", "pr:0-100"), btn("tot €250", "pr:0-250")],
            [btn("tot €500", "pr:0-500"), btn("tot €1000", "pr:0-1000")],
            [btn("💶 Geen limiet", "pr:none")]]
    return (f"{draft_title(d)}\nStap 3/3 — Welke prijs?\n"
            f"Tik een knop, of typ zelf bv. <code>100-300</code>, "
            f"<code>tot 300</code> of <code>vanaf 100</code>."), rows


def start_draft(state, query, excl=None):
    d = {"query": query, "sites": list(SITES), "km": None, "min": None, "max": None,
         "exclude": excl or [], "step": "sites"}
    text, rows = show_sites(d)
    d["msg"] = send(text, rows)
    state["draft"] = d


def finish_draft(state):
    d = state["draft"]
    state["draft"] = None
    s = add_search(state, d["query"], d["min"], d["max"], d["sites"], d["km"],
                   exclude=d.get("exclude"))
    text = f"✅ Toegevoegd:\n{describe(s)}\nIk zoek nu en meld daarna alleen nieuwe zoekertjes."
    if d.get("msg"):
        edit(d["msg"], text, [])
        return None
    return text


def handle_button(state, cq):
    data = cq.get("data", "")
    try:
        tg("answerCallbackQuery", callback_query_id=cq["id"])
    except Exception:
        pass
    d = state.get("draft")
    msg_id = (cq.get("message") or {}).get("message_id")
    if not d or msg_id != d.get("msg"):
        edit(msg_id, "⌛ Deze keuze is verlopen. Stuur je zoekopdracht opnieuw.", [])
        return
    if data == "cancel":
        state["draft"] = None
        edit(msg_id, f"{draft_title(d)}\n✖️ Geannuleerd.", [])
        return
    if data.startswith("p:"):
        site = data[2:]
        if site in d["sites"]:
            if len(d["sites"]) > 1:
                d["sites"].remove(site)
        else:
            d["sites"].append(site)
            d["sites"].sort(key=list(SITES).index)
        edit(msg_id, *show_sites(d))
        return
    if data == "next":
        if any(s in KM_SITES for s in d["sites"]):
            d["step"] = "km"
            edit(msg_id, *show_km(d))
        else:
            d["step"] = "price"
            edit(msg_id, *show_price(d))
        return
    if data.startswith("km:"):
        d["km"] = float(data[3:]) or None
        d["step"] = "price"
        edit(msg_id, *show_price(d))
        return
    if data.startswith("pr:"):
        if data != "pr:none":
            lo, hi = data[3:].split("-")
            d["min"], d["max"] = (float(lo) or None), float(hi)
        finish_draft(state)
        return


def handle_updates(state, wait=0):
    """Leest nieuwe berichten en knopdrukken; wacht tot 'wait' seconden (long polling)."""
    res = tg("getUpdates", offset=state["update_offset"], timeout=wait,
             allowed_updates=["message", "callback_query"])
    for u in res.get("result", []):
        state["update_offset"] = u["update_id"] + 1
        if "callback_query" in u:
            cq = u["callback_query"]
            if str((cq.get("message") or {}).get("chat", {}).get("id")) == CHAT_ID:
                handle_button(state, cq)
            continue
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != CHAT_ID:
            print("Bericht van een andere chat genegeerd")
            continue  # alleen jij mag de bot bedienen
        reply = run_command(state, (msg.get("text") or "").strip())
        if reply:
            send(reply, preview=False)
    return bool(res.get("result"))


# ---------------------------------------------------------------- hoofdlus

def report(state, errors):
    if errors and time.time() - state.get("last_error_notice", 0) > ERROR_NOTIFY_EVERY:
        send("⚠️ Zoeken lukte niet helemaal:\n" + html.escape("\n".join(errors[:5])))
        state["last_error_notice"] = time.time()


def run_once():
    state = load_state()
    try:
        handle_updates(state)
    except Exception as e:
        print(f"Kon Telegram-berichten niet ophalen: {e}", file=sys.stderr)
    save_state(state)  # commando's niet kwijtraken als het zoeken misloopt
    sent, errors = check_searches(state)
    report(state, errors)
    cleanup_messages(state)
    save_state(state)
    print(f"{len(state['searches'])} zoekopdracht(en), {sent} melding(en), {len(errors)} fout(en)")


def run_for(minutes, check_every=120):
    """Blijft 'minutes' minuten draaien: antwoordt meteen op berichten
    en checkt de sites om de 'check_every' seconden."""
    end = time.time() + minutes * 60
    state = load_state()
    last_check = 0
    while time.time() < end:
        try:
            changed = handle_updates(state, wait=min(30, max(1, int(end - time.time()))))
        except Exception as e:
            print(f"Kon Telegram-berichten niet ophalen: {e}", file=sys.stderr)
            changed = False
            time.sleep(10)
        new_search = any(not s.get("primed") for s in state["searches"])
        if (changed and new_search) or time.time() - last_check >= check_every:
            sent, errors = check_searches(state)
            last_check = time.time()
            report(state, errors)
            cleanup_messages(state)
            print(f"{len(state['searches'])} zoekopdracht(en), {sent} melding(en), {len(errors)} fout(en)")
        save_state(state)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, metavar="SECONDEN",
                    help="blijf checken met deze pauze (bv. 120)")
    ap.add_argument("--duur", type=float, metavar="MINUTEN",
                    help="blijf zoveel minuten draaien en antwoord meteen (voor GitHub)")
    args = ap.parse_args()
    if not TOKEN or not CHAT_ID:
        sys.exit("TELEGRAM_TOKEN en TELEGRAM_CHAT_ID moeten ingesteld zijn.")
    if args.duur:
        run_for(args.duur)
        return
    if not args.loop:
        run_once()
        return
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"Onverwachte fout: {e}", file=sys.stderr)
        time.sleep(max(30, args.loop))


if __name__ == "__main__":
    main()
