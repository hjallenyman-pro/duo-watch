#!/usr/bin/env python3
"""
duo_watch.py - bevakar Apples butiker nära dig och skickar en push-notis
till din iPhone när iPhone Duo finns att hämta i butik.

Testa en gång:   python3 duo_watch.py --test
Kör bevakningen: caffeinate -i python3 duo_watch.py
(caffeinate hindrar Macen från att somna medan boten körs)

Behöver inget att installeras, bara Python 3.
"""

import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

# ------------------------- INSTÄLLNINGAR -------------------------
# Värdena kan sättas som miljövariabler (används i molnet), annars gäller standardvärdet.
ZIP = os.environ.get("DUO_ZIP", "")
NTFY_TOPIC = os.environ.get("DUO_TOPIC", "")  # samma namn som i ntfy-appen
POLL_SECONDS = 300                  # hur ofta vi kollar (5 min). Gå inte under 120.
MAX_MILES = None                    # t.ex. 90 för att bara bry dig om butiker inom 90 miles

# Artikelnummer (USA) -> (namn, sidan på apple.com som notisen öppnar)
BASE = "https://www.apple.com/shop/buy-iphone/iphone-duo/"
VARIANTS = {
    "MK1F4LL/A": ("Night Sky 256 GB",  "7.6-inch-display-256gb-night-sky-unlocked"),
    "MK1E4LL/A": ("Star White 256 GB", "7.6-inch-display-256gb-star-white-unlocked"),
    "MK1H4LL/A": ("Night Sky 512 GB",  "7.6-inch-display-512gb-night-sky-unlocked"),
    "MK1G4LL/A": ("Star White 512 GB", "7.6-inch-display-512gb-star-white-unlocked"),
}
# ------------------------------------------------------------------

STATE_DIR = os.environ.get("DUO_STATE_DIR", os.path.dirname(os.path.abspath(__file__)))
STATE_FILE = os.path.join(STATE_DIR, "duo_state.json")
FAILS_FILE = os.path.join(STATE_DIR, "duo_fails.json")
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.0 Safari/605.1.15"
)


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


FULL_EXTRA = [("pl", "true"), ("mts.0", "regular"), ("mts.1", "compact"), ("searchNearby", "true")]


def build_url(parts, extra):
    params = list(extra) + [("location", ZIP)]
    for i, part in enumerate(parts):
        params.append((f"parts.{i}", part))
    return "https://www.apple.com/shop/fulfillment-messages?" + urllib.parse.urlencode(params)


def get_json(url):
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Referer": "https://www.apple.com/shop/buy-iphone/iphone-duo",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:300].replace("\n", " ")
        except Exception:
            pass
        raise RuntimeError(f"HTTP {e.code} {e.reason}: {body}") from None


def fetch():
    """Frågar Apple om hämta-i-butik-status för alla varianter nära ZIP."""
    return get_json(build_url(list(VARIANTS), FULL_EXTRA))


def diagnose():
    """Provar olika varianter av anropet för att se vad Apple accepterar."""
    log("Felsökning: provar olika varianter av anropet ...")
    first = next(iter(VARIANTS))
    tests = [("alla varianter, fulla parametrar", list(VARIANTS), FULL_EXTRA),
             ("en variant, fulla parametrar", [first], FULL_EXTRA),
             ("en variant, bara postnummer", [first], []),
             ("en variant, utan searchNearby", [first], [("mts.0", "regular"), ("mts.1", "compact")])]
    for part, (label, _) in VARIANTS.items():
        tests.append((f"bara {label}, minimalt anrop", [part], []))
    for label, parts, extra in tests:
        try:
            data = get_json(build_url(parts, extra))
            stores = data.get("body", {}).get("content", {}).get("pickupMessage", {}).get("stores")
            log(f"OK   {label}: {len(stores) if stores is not None else '?'} butiker")
        except Exception as e:
            log(f"FEL  {label}: {e}")
        time.sleep(2)


def parse(data):
    """Plockar ut en rad per (butik, variant) ur Apples svar."""
    try:
        stores = data["body"]["content"]["pickupMessage"]["stores"]
    except (KeyError, TypeError):
        raise ValueError("Oväntat svar från Apple: " + json.dumps(data)[:300])

    rows = []
    for s in stores:
        distance_text = s.get("storeDistanceWithUnit") or ""
        miles = s.get("storeDistance")
        try:
            miles = float(miles)
        except (TypeError, ValueError):
            miles = None
        if MAX_MILES is not None and miles is not None and miles > MAX_MILES:
            continue

        for part, info in (s.get("partsAvailability") or {}).items():
            title = (
                info.get("storePickupProductTitle")
                or (info.get("messageTypes") or {}).get("regular", {}).get("storePickupProductTitle")
                or ""
            )
            rows.append(
                {
                    "store_id": str(s.get("storeNumber") or s.get("storeName")),
                    "store": s.get("storeName", "okänd butik"),
                    "where": ", ".join(x for x in (s.get("city"), s.get("state")) if x),
                    "distance": distance_text,
                    "part": part,
                    "status": info.get("pickupDisplay", "okänt"),
                    "title": title,
                }
            )
    return rows


def send_push(title, body, click=None, priority="3", tags="iphone"):
    """Skickar en push via ntfy.sh. Rubriken (title) ska vara ren ASCII."""
    headers = {"Title": title, "Priority": priority, "Tags": tags}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20):
            pass
    except Exception as e:  # notisen får aldrig krascha boten
        log(f"Kunde inte skicka notis: {e}")


def notify_available(row):
    label, slug = VARIANTS.get(row["part"], (row["part"], ""))
    dist = f", {row['distance']}" if row["distance"] else ""
    body = f"{label} finns att hämta i {row['store']} ({row['where']}{dist}). Var snabb, de tar slut fort!"
    log("TILLGÄNGLIG: " + body)
    send_push(
        "iPhone Duo finns i butik",
        body,
        click=BASE + slug if slug else None,
        priority="5",
        tags="tada",
    )


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        log(f"Kunde inte spara status: {e}")


def check(rows, old_state, notify=notify_available):
    """Notifierar bara när något går från 'inte tillgänglig' till 'tillgänglig'."""
    new_state = {}
    for r in rows:
        key = f"{r['store_id']}|{r['part']}"
        is_available = r["status"] == "available"
        new_state[key] = is_available
        if is_available and not old_state.get(key, False):
            notify(r)
    return new_state


def run_test():
    log(f"Testar mot Apple för postnummer {ZIP} ...")
    try:
        rows = parse(fetch())
    except Exception as e:
        log(f"Felet: {e}")
        diagnose()
        send_push("Duo-boten: testet misslyckades", str(e)[:200], priority="3", tags="warning")
        sys.exit(1)
    if not rows:
        log("Apple svarade, men inga butiker/varianter hittades. Kolla ZIP och artikelnummer.")
    else:
        stores = sorted({(r["store"], r["distance"]) for r in rows})
        log(f"Hittade {len(stores)} butiker:")
        for name, dist in stores:
            print(f"   - {name} {dist}")
        print()
        for r in rows:
            label = VARIANTS.get(r["part"], (r["part"],))[0]
            print(f"   {r['store']:<28} {label:<18} {r['status']:<14} {r['title']}")
        seen = {r["part"] for r in rows}
        for part, (label, _) in VARIANTS.items():
            if part not in seen:
                log(f"OBS: {label} ({part}) kom inte med i svaret. Artikelnumret kan vara fel.")
    send_push("Duo-boten: test", "Om du ser det här fungerar notiserna.", priority="3")
    log("Testnotis skickad. Kolla din iPhone.")


def run_once():
    """En enda kontroll. Används av GitHub Actions, som startar skriptet var 5:e minut."""
    os.makedirs(STATE_DIR, exist_ok=True)
    try:
        with open(FAILS_FILE) as f:
            fails = int(json.load(f).get("fails", 0))
    except Exception:
        fails = 0
    try:
        rows = parse(fetch())
        save_state(check(rows, load_state()))
        available = sum(1 for r in rows if r["status"] == "available")
        log(f"{len(rows)} kontroller, {available} tillgängliga.")
        fails = 0
        code = 0
    except Exception as e:
        fails += 1
        log(f"Fel ({fails} i rad): {e}")
        if fails == 6:  # ca en halvtimme av fel, varna en gång
            send_push("Duo-boten har problem", f"Sex fel i rad: {str(e)[:150]}", priority="3", tags="warning")
        code = 0
    with open(FAILS_FILE, "w") as f:
        json.dump({"fails": fails}, f)
    sys.exit(code)


def main():
    if not ZIP or not NTFY_TOPIC:
        sys.exit("DUO_ZIP och DUO_TOPIC måste vara satta.")
    if "--test" in sys.argv:
        run_test()
        if "--once" not in sys.argv:
            return
    if "--once" in sys.argv:
        run_once()
        return

    state = load_state()
    fails = 0
    warned = False
    log(f"Bevakar iPhone Duo nära {ZIP} var {POLL_SECONDS // 60}:e minut. Avsluta med Ctrl+C.")

    while True:
        try:
            rows = parse(fetch())
            state = check(rows, state)
            save_state(state)
            available = sum(1 for r in rows if r["status"] == "available")
            log(f"{len(rows)} kontroller, {available} tillgängliga.")
            fails = 0
            warned = False
            wait = POLL_SECONDS
        except KeyboardInterrupt:
            raise
        except Exception as e:
            fails += 1
            log(f"Fel ({fails} i rad): {e}")
            if fails >= 5 and not warned:
                send_push("Duo-boten har problem", f"Fem fel i rad: {str(e)[:150]}", priority="3", tags="warning")
                warned = True
            wait = min(POLL_SECONDS * (2 ** min(fails, 3)), 1800)  # backa av vid fel
        time.sleep(wait + random.randint(0, 60))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Avslutad.")
