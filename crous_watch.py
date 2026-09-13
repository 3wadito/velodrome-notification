#!/usr/bin/env python3
"""
Watch trouverunlogement.lescrous.fr for Crous housing at VELODROME and
CHARMOIS (both Vandoeuvre-les-Nancy) and push a phone notification the
moment one appears.

Design notes:
  - The Crous search page is server-side rendered, so a plain GET returns the
    full list of listings. No JS engine needed.
  - The campaign ("tool") id changes between phases, so it is resolved at
    runtime from /api/fr/tools instead of being hardcoded.
  - Matching is accent-insensitive: the site writes VELODROME unaccented in
    some places and Velodrome elsewhere, so accents are stripped on both
    sides before comparing.
  - State lives in known.json so a listing is announced once, not every cycle.

  - HEALTH ALARM: at a 5 second interval this sends roughly 2000 requests per
    hour from one IP. The likely failure is not a crash but a silent block
    (403/429) or a markup change, either of which looks exactly like "no
    rooms available". So consecutive failures, and consecutive cycles where
    the site reports zero listings nationally, each raise a push alarm. A
    quiet phone only means something if you would be told when the watcher
    goes blind.

Env vars:
  NTFY_TOPIC      required, your ntfy topic string (treat as a password)
  KEYWORDS        optional, comma separated. Default: VELODROME,CHARMOIS
  POLL_SECONDS    optional, seconds between cycles. Default 5.
  RUN_SECONDS     optional, total run duration. Default 0 = single pass.
  ALARM_AFTER     optional, consecutive bad cycles before alarming. Default 3.
"""

import json
import os
import re
import sys
import time
import unicodedata
from pathlib import Path

import requests
from bs4 import BeautifulSoup

BASE = "https://trouverunlogement.lescrous.fr"
STATE = Path(__file__).with_name("known.json")
BEAT = Path(__file__).with_name("heartbeat.json")
UA = "crous-velodrome-watch/2.0 (personal availability alert)"


def deaccent(s):
    """Accent-insensitive uppercase, so VELODROME matches Velodrome."""
    return "".join(
        c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn"
    ).upper()


KEYWORDS = [
    deaccent(k.strip())
    for k in os.environ.get("KEYWORDS", "VELODROME,CHARMOIS").split(",")
    if k.strip()
]
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
POLL_SECONDS = float(os.environ.get("POLL_SECONDS", "5"))
RUN_SECONDS = int(os.environ.get("RUN_SECONDS", "0"))
ALARM_AFTER = int(os.environ.get("ALARM_AFTER", "3"))
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "12"))


class Blocked(Exception):
    """Server actively refused us: rate limit or WAF."""


# Each listing card is a flat run of short text nodes. Rather than guess at
# their order (which differs between listing types), classify each node by
# what it looks like: a euro amount, a m2 figure, a French postcode, an
# occupancy word, a bed count.
RE_PRICE = re.compile(r"\d[\d\s  .,]*€")
RE_SURFACE = re.compile(r"\d[\d,.]*\s*m²")
RE_POSTCODE = re.compile(r"\b\d{5}\b")
OCCUPANCY = ("INDIVIDUEL", "COLOCATION", "COUPLE")


def parse_card(card):
    """Pull price / surface / address / occupancy out of one listing card."""
    parts = [p for p in card.stripped_strings if p.strip()]
    rec = {
        "price": "",
        "surface": "",
        "address": "",
        "occupancy": "",
        "beds": "",
        "text": " ".join(" ".join(parts).split()),
    }
    for p in parts:
        up = deaccent(p)
        if not rec["price"] and RE_PRICE.search(p):
            rec["price"] = " ".join(p.split())
        elif not rec["surface"] and RE_SURFACE.search(p):
            rec["surface"] = " ".join(p.split())
        elif not rec["address"] and RE_POSTCODE.search(p):
            rec["address"] = " ".join(p.split())
        elif not rec["occupancy"] and any(o in up for o in OCCUPANCY):
            rec["occupancy"] = " ".join(p.split())
        elif not rec["beds"] and "LIT" in up:
            rec["beds"] = " ".join(p.split())
    return rec


def format_listing(v):
    """Readable notification body: price first, it is what you decide on."""
    lines = []
    if v.get("price"):
        lines.append(f"💶 {v['price']}")
    spec = " · ".join(x for x in (v.get("surface"), v.get("occupancy")) if x)
    if spec:
        lines.append(spec)
    if v.get("beds"):
        lines.append(v["beds"])
    if v.get("address"):
        lines.append(f"📍 {v['address']}")
    if not lines:
        lines.append(v.get("text", "")[:200])
    return "\n".join(lines)


def session():
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "fr-FR,fr;q=0.9"})
    return s


def active_tool_id(s):
    r = s.get(f"{BASE}/api/fr/tools", timeout=20)
    if r.status_code in (403, 429, 503):
        raise Blocked(f"HTTP {r.status_code} on /api/fr/tools")
    r.raise_for_status()
    tools = [t for t in r.json() if t.get("enabled") and t.get("published")]
    if not tools:
        raise RuntimeError("no active Crous campaign right now")
    return max(tools, key=lambda t: t["id"])["id"]


def fetch_listings(s, tool_id, max_pages=25):
    out = {}
    page = 1
    while page <= max_pages:
        r = s.get(f"{BASE}/tools/{tool_id}/search?page={page}", timeout=30)
        if r.status_code in (403, 429, 503):
            raise Blocked(f"HTTP {r.status_code} on page {page}")
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        cards = soup.select("div.fr-card")
        if not cards:
            break
        found = 0
        for card in cards:
            link = card.find("a", href=re.compile(r"/accommodations/\d+"))
            if not link:
                continue
            lid = re.search(r"/accommodations/(\d+)", link["href"]).group(1)
            rec = parse_card(card)
            rec.update(
                id=lid,
                name=link.get_text(strip=True),
                url=f"{BASE}/tools/{tool_id}/accommodations/{lid}",
            )
            out[lid] = rec
            found += 1
        if found == 0:
            break
        page += 1
    return out


def matches(listing):
    blob = deaccent(listing["name"] + " " + listing["text"])
    return any(k in blob for k in KEYWORDS)


def load_state():
    if STATE.exists():
        try:
            return set(json.loads(STATE.read_text()))
        except Exception:
            return set()
    return set()


def save_state(ids):
    STATE.write_text(json.dumps(sorted(ids), indent=0))


def heartbeat(listings_count, hits_count):
    """Periodic proof-of-life.

    Silence is ambiguous: a healthy watcher with nothing to report looks
    exactly like a dead one. The BLIND alarm only speaks while the script is
    running, so it cannot report a crashed or never-launched job. This sends
    a low-priority ping every HEARTBEAT_HOURS. If one fails to arrive on
    schedule, the chain is broken somewhere the script itself cannot see.

    The timestamp lives in a committed file so the interval survives the
    hourly restart.
    """
    if HEARTBEAT_HOURS <= 0:
        return
    now = time.time()
    last = 0.0
    if BEAT.exists():
        try:
            last = float(json.loads(BEAT.read_text()).get("last", 0))
        except Exception:
            last = 0.0
    if now - last < HEARTBEAT_HOURS * 3600:
        return
    notify(
        "Watcher alive",
        f"Still checking every {int(POLL_SECONDS)}s.\n"
        f"{listings_count} listings nationally, {hits_count} at "
        f"{'/'.join(KEYWORDS)}.",
        priority="min",
        tags="green_heart",
    )
    BEAT.write_text(json.dumps({"last": now}))


def notify(title, body, url=None, priority="urgent", tags="house,rotating_light"):
    if not NTFY_TOPIC:
        print(f"[no NTFY_TOPIC] {title} :: {body}")
        return
    headers = {"Title": title.encode("utf-8"), "Priority": priority, "Tags": tags}
    if url:
        headers["Click"] = url
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=body.encode("utf-8"),
            headers=headers,
            timeout=15,
        )
    except Exception as e:
        print(f"notify failed: {e}", file=sys.stderr)


class Health:
    """One alarm per fault episode, one all-clear on recovery."""

    def __init__(self):
        self.bad = 0
        self.alarmed = False

    def ok(self):
        if self.alarmed:
            notify(
                "Watcher recovered",
                "Site responding normally again. Monitoring resumed.",
                priority="low",
                tags="white_check_mark",
            )
        self.bad = 0
        self.alarmed = False

    def fault(self, reason):
        self.bad += 1
        print(f"fault ({self.bad}): {reason}", file=sys.stderr)
        if self.bad >= ALARM_AFTER and not self.alarmed:
            notify(
                "WATCHER IS BLIND",
                f"{self.bad} consecutive bad cycles: {reason}\n"
                "You are NOT being monitored. Likely an IP block (raise "
                "POLL_SECONDS) or the site markup changed.",
                priority="urgent",
                tags="warning",
            )
            self.alarmed = True


def cycle(s, known, seeded, health):
    try:
        tool_id = active_tool_id(s)
        listings = fetch_listings(s, tool_id)
    except Blocked as e:
        health.fault(str(e))
        return known, seeded, True
    except Exception as e:
        health.fault(f"{type(e).__name__}: {e}")
        return known, seeded, False

    # Zero nationally is possible but rare; sustained zero almost always
    # means a broken parser rather than a genuinely empty country.
    if not listings:
        health.fault("0 listings parsed nationally")
        return known, seeded, False

    health.ok()

    hits = {k: v for k, v in listings.items() if matches(v)}
    print(f"tool={tool_id} total={len(listings)} matching={len(hits)}")
    heartbeat(len(listings), len(hits))

    if not seeded:
        known.update(hits.keys())
        notify(
            "Crous watcher armed",
            f"Watching: {', '.join(KEYWORDS)}\n"
            f"{len(listings)} listings nationally, {len(hits)} at your "
            f"residences (already present, not alerted).",
            priority="low",
            tags="satellite",
        )
        return known, True, False

    for v in (hits[k] for k in hits if k not in known):
        notify(
            f"DISPO: {v['name']}",
            f"{format_listing(v)}\n\n{v['url']}",
            url=v["url"],
        )
        print(f"NEW: {v['name']} | {v.get('price')} | {v.get('surface')} -> {v['url']}")

    known = (known & set(listings.keys())) | set(hits.keys())
    return known, True, False


def main():
    s = session()
    known = load_state()
    seeded = bool(known)
    health = Health()
    deadline = time.time() + RUN_SECONDS if RUN_SECONDS else None
    backoff = 0

    while True:
        known, seeded, blocked = cycle(s, known, seeded, health)
        save_state(known)

        if deadline is None or time.time() >= deadline:
            break

        # Back off hard when refused, so a rate limit does not become a ban.
        if blocked:
            backoff = min(300, max(30, backoff * 2))
            print(f"backing off {backoff}s")
            time.sleep(backoff)
        else:
            backoff = 0
            time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
