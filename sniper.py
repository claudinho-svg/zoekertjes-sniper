#!/usr/bin/env python3
"""
Zoekertjes-sniper: houdt 2dehands.be, Marktplaats.nl, Vinted.be en
Kleinanzeigen.de in de gaten en stuurt nieuwe zoekertjes naar Telegram.

Bediening via Telegram (gewoon typen, slash mag maar hoeft niet):
  iphone 15                        -> zoekt op alle vier de sites
  iphone 15 tot 500                -> alleen zoekertjes tot 500 euro
  nike dunk tot 80 op vinted
  ps5 op 2dehands en kleinanzeigen
  fiets tot 200 binnen 15 km       -> rond je postcode, 2dehands + Marktplaats
  lijst                            -> toont je zoekopdrachten
  stop 2                           -> verwijdert zoekopdracht nr. 2
  help

Draaien:
  python3 sniper.py               -> één keer checken (voor GitHub Actions)
  python3 sniper.py --loop 120    -> blijft checken, elke 120 seconden (eigen computer)

Nodig: omgevingsvariabelen TELEGRAM_TOKEN en TELEGRAM_CHAT_ID.
Gebruikt alleen de standaardbibliotheek van Python, niets te installeren.
"""
import argparse
import html
import http.cookiejar
import json
import math
import os
import re
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
SITE_RE = "2dehands|marktplaats|vinted|kleinanzeigen"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36"
)
MAX_SEEN_PER_SEARCH = 600
MAX_ALERTS_PER_RUN = 15          # voorkomt een spervuur aan berichten
ERROR_NOTIFY_EVERY = 6 * 3600    # foutmelding hoogstens om de 6 uur

TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
POSTCODE = os.environ.get("POSTCODE", "").strip()   # geheim, voor "binnen 20 km"
try:  # geheim, "lat,lon" van thuis: afstand voor Marktplaats (kent geen BE-postcodes)
    HOME = tuple(float(x) for x in os.environ.get("LOCATIE", "").split(","))[:2] or None
except ValueError:
    HOME = None


# ---------------------------------------------------------------- status

def load_state():
    state = {"searches": [], "seen": {}, "update_offset": 0, "next_id": 1,
             "last_error_notice": 0}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state.update(json.load(f))
    return state


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE_FILE)


# ---------------------------------------------------------------- http

def http_json(url, data=None, timeout=25):
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA,
        "Accept": "application/json",
        "Accept-Language": "nl-BE,nl;q=0.9",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def tg(method, **params):
    data = urllib.parse.urlencode(params).encode()
    wait = int(params.get("timeout", 0))
    return http_json(f"https://api.telegram.org/bot{TOKEN}/{method}", data, timeout=wait + 25)


def send(text):
    try:
        tg("sendMessage", chat_id=CHAT_ID, text=text, parse_mode="HTML")
    except urllib.error.HTTPError as e:
        print(f"Telegram-fout: {e} {e.read()[:300]!r}", file=sys.stderr)


# ---------------------------------------------------------------- zoeken

PRICE_TYPES = {
    "FAST_BID": "Bieden", "MIN_BID": "Bieden", "BID": "Bieden",
    "FREE": "Gratis", "SEE_DESCRIPTION": "Zie omschrijving",
    "RESERVED": "Gereserveerd", "EXCHANGE": "Ruilen", "ON_REQUEST": "Op aanvraag",
    "VB": "Bieden (VB)", "Zu verschenken": "Gratis",
}


_vinted = None


def vinted_opener():
    """Vinted wil eerst een sessiecookie van de homepage."""
    global _vinted
    if _vinted is None:
        op = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        op.addheaders = [("User-Agent", UA), ("Accept-Language", "nl-BE,nl;q=0.9")]
        op.open(SITES["vinted"] + "/", timeout=25).read()
        _vinted = op
    return _vinted


def search_vinted(query):
    params = {"search_text": query, "order": "newest_first", "per_page": 30, "page": 1}
    url = SITES["vinted"] + "/api/v2/catalog/items?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with vinted_opener().open(req, timeout=25) as r:
        data = json.load(r)
    items = []
    for it in data.get("items", []):
        if not it.get("id"):
            continue
        price = it.get("price")
        amount = price.get("amount") if isinstance(price, dict) else price
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            amount = None
        items.append({
            "id": f"v{it['id']}",
            "site": "vinted",
            "title": (it.get("title") or "").strip(),
            "price": amount,
            "price_type": "",
            "city": "",
            "url": it.get("url") or f"{SITES['vinted']}/items/{it['id']}",
        })
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
        price_txt = _clean(price_m.group(1)) if price_m else ""
        num = re.search(r"(\d[\d.]*)\s*€", price_txt)
        items.append({
            "id": f"k{ad_id}",
            "site": "kleinanzeigen",
            "title": _clean(title.group(1)) if title else "",
            "price": float(num.group(1).replace(".", "")) if num else None,
            "price_type": "" if num else (price_txt or "SEE_DESCRIPTION"),
            "city": _clean(loc.group(1)) if loc else "",
            "url": SITES["kleinanzeigen"] + (href.group(1) if href else f"/s-anzeige/{ad_id}"),
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
            "id": item_id,
            "site": site,
            "title": l.get("title", "").strip(),
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


def price_text(item):
    if item["price"] is not None:
        p = item["price"]
        return f"€ {p:,.0f}".replace(",", ".") if p == int(p) else f"€ {p:.2f}"
    pt = item["price_type"]
    return PRICE_TYPES.get(pt, pt if pt and not pt.isupper() else "Prijs onbekend")


def matches(search, item):
    mx = search.get("max_price")
    if mx is not None and item["price"] is not None and item["price"] > mx:
        return False
    km = search.get("km")
    if km and item["site"] == "marktplaats":
        return item.get("dist") is not None and item["dist"] <= km
    return True


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
        if km and not pc:
            errors.append(f"'{s['query']}': geen postcode ingesteld voor 'binnen {km:g} km'")
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

        if not s.get("primed"):
            # eerste keer: bestaande zoekertjes alleen onthouden, niet melden
            if found:
                s["primed"] = True
            continue

        for i in new:
            if not matches(s, i) or sent >= MAX_ALERTS_PER_RUN:
                continue
            dist = f"{i['dist']:.0f} km" if i.get("dist") is not None else ""
            info = " · ".join(x for x in (price_text(i), html.escape(i["city"]), dist, i["site"]) if x)
            send(
                f"🎯 <b>{html.escape(i['title'])}</b>\n"
                f"{info}\n"
                f"Zoekopdracht: {html.escape(s['query'])}\n"
                f"{html.escape(i['url'])}"
            )
            sent += 1

        state["seen"][key] = seen[-MAX_SEEN_PER_SEARCH:]
    return sent, errors


# ---------------------------------------------------------------- commando's

HELP = (
    "<b>Zoekertjes-sniper</b>\n"
    "Stuur gewoon wat je zoekt, bv.:\n"
    "iphone 15 — zoekt op 2dehands, Marktplaats, Vinted en Kleinanzeigen\n"
    "iphone 15 tot 500 — met maximumprijs\n"
    "nike dunk tot 80 op vinted — maar één site\n"
    "ps5 op 2dehands en vinted — twee sites\n"
    "fiets tot 200 binnen 15 km — rond je postcode (2dehands + Marktplaats)\n\n"
    "lijst — je zoekopdrachten\n"
    "stop 2 — zoekopdracht 2 verwijderen\n\n"
    "Je krijgt alleen nieuwe zoekertjes; wat er al stond meld ik niet."
)


def parse_zoek(arg):
    """Geeft (zoekterm, maxprijs, sites, km, postcode) terug."""
    sites, max_price, km, pc, explicit = list(SITES), None, None, None, False
    for _ in range(3):  # 'tot', 'op' en 'binnen' in om het even welke volgorde
        m = re.search(r"\s+binnen\s+(\d+(?:[.,]\d+)?)\s*km(?:\s+(?:van|rond)\s+(\w+))?\s*$", arg, re.I)
        if m:
            km = float(m.group(1).replace(",", "."))
            pc = m.group(2)
            arg = arg[:m.start()]
        m = re.search(rf"\s+op\s+((?:{SITE_RE})(?:\s*(?:,|\+|en)\s*(?:{SITE_RE}))*)\s*$", arg, re.I)
        if m:
            sites = list(dict.fromkeys(re.findall(SITE_RE, m.group(1).lower())))
            explicit = True
            arg = arg[:m.start()]
        m = re.search(r"\s+tot\s+€?\s*(\d+(?:[.,]\d+)?)\s*(?:euro|eur|€)?\s*$", arg, re.I)
        if m:
            max_price = float(m.group(1).replace(",", "."))
            arg = arg[:m.start()]
    if km and not explicit:
        sites = ["2dehands", "marktplaats"]  # sites waar afstand werkt
    return arg.strip(), max_price, sites, km, pc


def describe(s):
    extra = f" tot € {s['max_price']:g}" if s.get("max_price") is not None else ""
    if s.get("km"):
        extra += f" binnen {s['km']:g} km"
    sites = " + ".join(s["sites"])
    return f"{s['id']}. {html.escape(s['query'])}{extra} ({sites})"


def run_command(state, text):
    if not text:
        return None
    parts = text.split(maxsplit=1)
    cmd = parts[0].split("@")[0].lower().lstrip("/")
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd not in ("start", "help", "zoek", "lijst", "stop"):
        cmd, arg = "zoek", text  # gewone tekst = nieuwe zoekopdracht
    cmd = "/" + cmd

    if cmd in ("/start", "/help"):
        return HELP

    if cmd == "/zoek":
        query, max_price, sites, km, pc = parse_zoek(arg)
        if not query:
            return "Wat moet ik zoeken? Bv. iphone 15 tot 500"
        s = {"id": state["next_id"], "query": query, "max_price": max_price,
             "sites": sites, "km": km, "postcode": pc, "primed": False}
        state["next_id"] += 1
        state["searches"].append(s)
        return f"✅ Toegevoegd:\n{describe(s)}\nIk meld vanaf nu nieuwe zoekertjes."

    if cmd == "/lijst":
        if not state["searches"]:
            return "Nog geen zoekopdrachten. Stuur bv. iphone 15 tot 500"
        return "<b>Je zoekopdrachten</b>\n" + "\n".join(describe(s) for s in state["searches"])

    if cmd == "/stop":
        if not arg.isdigit():
            return "Welke? Bv. stop 2 (nummers zie je met lijst)"
        n = int(arg)
        before = len(state["searches"])
        state["searches"] = [s for s in state["searches"] if s["id"] != n]
        state["seen"].pop(str(n), None)
        return f"🗑️ Zoekopdracht {n} verwijderd." if len(state["searches"]) < before \
            else f"Zoekopdracht {n} bestaat niet. Zie lijst."

    return HELP


def handle_commands(state, wait=0):
    """Leest nieuwe berichten; wacht tot `wait` seconden op een bericht (long polling)."""
    res = tg("getUpdates", offset=state["update_offset"], timeout=wait)
    if res.get("result"):
        print(f"{len(res['result'])} Telegram-bericht(en) ontvangen")
    for u in res.get("result", []):
        state["update_offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        if str((msg.get("chat") or {}).get("id")) != CHAT_ID:
            print("Bericht van een andere chat genegeerd")
            continue  # alleen jij mag de bot bedienen
        reply = run_command(state, (msg.get("text") or "").strip())
        if reply:
            send(reply)
    return bool(res.get("result"))


# ---------------------------------------------------------------- hoofdlus

def run_once():
    state = load_state()
    try:
        handle_commands(state)
    except Exception as e:
        print(f"Kon Telegram-berichten niet ophalen: {e}", file=sys.stderr)
    save_state(state)  # commando's niet kwijtraken als het zoeken misloopt

    sent, errors = check_searches(state)
    if errors and time.time() - state.get("last_error_notice", 0) > ERROR_NOTIFY_EVERY:
        send("⚠️ Zoeken lukte niet helemaal:\n" + html.escape("\n".join(errors[:5])))
        state["last_error_notice"] = time.time()
    save_state(state)
    print(f"{len(state['searches'])} zoekopdracht(en), {sent} melding(en), {len(errors)} fout(en)")


def run_for(minutes, check_every=120):
    """Blijft `minutes` minuten draaien: antwoordt meteen op berichten
    en checkt de sites om de `check_every` seconden."""
    end = time.time() + minutes * 60
    state = load_state()
    last_check = 0
    while time.time() < end:
        try:
            changed = handle_commands(state, wait=min(30, max(1, int(end - time.time()))))
        except Exception as e:
            print(f"Kon Telegram-berichten niet ophalen: {e}", file=sys.stderr)
            changed = False
            time.sleep(10)
        if changed or time.time() - last_check >= check_every:
            sent, errors = check_searches(state)
            last_check = time.time()
            if errors and time.time() - state.get("last_error_notice", 0) > ERROR_NOTIFY_EVERY:
                send("⚠️ Zoeken lukte niet helemaal:\n" + html.escape("\n".join(errors[:5])))
                state["last_error_notice"] = time.time()
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
