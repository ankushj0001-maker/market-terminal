#!/usr/bin/env python3
"""
fetch_data.py
Runs on GitHub Actions (server-side, not the user's browser/network), so it
talks to Yahoo Finance and the RSS feeds directly - no CORS proxy needed at
all, since CORS only restricts browser-initiated requests, not server calls.

Writes data.json, which the static dashboard (index.html) reads via a plain
same-origin fetch('./data.json') - no live cross-network call from the
visitor's browser, so nothing on their network can block it.

If any single source fails on a given run, this script keeps the last known
good value for that source (read from the existing data.json) instead of
wiping it out, so a transient failure never blanks the dashboard.
"""

import json
import os
import sys
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
TIMEOUT = 12
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

INDICES = [
    {"id": "nifty50",   "symbol": "^NSEI",     "kind": "index"},
    {"id": "sensex",    "symbol": "^BSESN",    "kind": "index"},
    {"id": "banknifty", "symbol": "^NSEBANK",  "kind": "index"},
    {"id": "vix",       "symbol": "^INDIAVIX", "kind": "index"},
    {"id": "usdinr",    "symbol": "INR=X",     "kind": "fx"},
    {"id": "gold",      "symbol": "GC=F",      "kind": "commodity"},
    {"id": "crude",     "symbol": "CL=F",      "kind": "commodity"},
    {"id": "us10y",     "symbol": "^TNX",      "kind": "yield"},
]

FEEDS = [
    {"id": "et",  "label": "Economic Times",        "url": "https://economictimes.indiatimes.com/rssfeedsdefault.cms"},
    {"id": "mc",  "label": "Moneycontrol",           "url": "https://www.moneycontrol.com/rss/latestnews.xml"},
    {"id": "lm",  "label": "LiveMint",                "url": "https://www.livemint.com/rss/markets"},
    {"id": "bs",  "label": "Business Standard",       "url": "https://www.business-standard.com/rss/markets-106.rss"},
    {"id": "rbi", "label": "Reserve Bank of India",   "url": "https://www.rbi.org.in/pressreleases_rss.xml"},
]


def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read()


def fetch_index(cfg):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{cfg['symbol']}?range=1d&interval=5m"
    raw = http_get(url)
    data = json.loads(raw)
    result = (data.get("chart") or {}).get("result") or []
    if not result:
        raise ValueError("empty result")
    meta = result[0].get("meta") or {}
    price = meta.get("regularMarketPrice")
    prev = meta.get("previousClose", meta.get("chartPreviousClose"))
    if not isinstance(price, (int, float)) or not isinstance(prev, (int, float)) or prev == 0:
        raise ValueError("missing price fields")
    if cfg["kind"] == "yield" and price > 15:
        price, prev = price / 10, prev / 10
    change = price - prev
    pct = (change / prev) * 100
    quote = ((result[0].get("indicators") or {}).get("quote") or [{}])[0]
    closes = [c for c in (quote.get("close") or []) if isinstance(c, (int, float))]
    return {
        "price": round(price, 4),
        "change": round(change, 4),
        "pct": round(pct, 4),
        "closes": closes[-60:],
        "ok": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def fetch_feed(feed):
    raw = http_get(feed["url"])
    root = ET.fromstring(raw)
    items = []
    for item in root.iter("item"):
        title_el = item.find("title")
        link_el = item.find("link")
        date_el = item.find("pubDate")
        title = (title_el.text or "").strip() if title_el is not None else ""
        link = (link_el.text or "#").strip() if link_el is not None else "#"
        date_text = (date_el.text or "").strip() if date_el is not None else ""
        try:
            pub_dt = parsedate_to_datetime(date_text) if date_text else datetime.now(timezone.utc)
            if pub_dt.tzinfo is None:
                pub_dt = pub_dt.replace(tzinfo=timezone.utc)
        except Exception:
            pub_dt = datetime.now(timezone.utc)
        if title:
            items.append({"title": title, "link": link, "pubDate": pub_dt.isoformat()})
    if not items:
        raise ValueError("no items parsed")
    items.sort(key=lambda x: x["pubDate"], reverse=True)
    return {
        "ok": True,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "items": items[:30],
    }


def load_previous():
    if os.path.exists(DATA_FILE):
        try:
            with open(DATA_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def main():
    previous = load_previous()
    prev_indices = previous.get("indices", {})
    prev_feeds = previous.get("feeds", {})

    out_indices = {}
    for cfg in INDICES:
        try:
            out_indices[cfg["id"]] = fetch_index(cfg)
            print(f"[ok]   index {cfg['id']}")
        except Exception as e:
            print(f"[fail] index {cfg['id']}: {e}", file=sys.stderr)
            fallback = prev_indices.get(cfg["id"])
            if fallback:
                fallback["ok"] = False
                out_indices[cfg["id"]] = fallback
            else:
                out_indices[cfg["id"]] = {"price": None, "change": None, "pct": None, "closes": [], "ok": False, "updated_at": None}

    out_feeds = {}
    for feed in FEEDS:
        try:
            out_feeds[feed["id"]] = fetch_feed(feed)
            print(f"[ok]   feed {feed['id']}")
        except Exception as e:
            print(f"[fail] feed {feed['id']}: {e}", file=sys.stderr)
            fallback = prev_feeds.get(feed["id"])
            if fallback:
                fallback["ok"] = False
                out_feeds[feed["id"]] = fallback
            else:
                out_feeds[feed["id"]] = {"ok": False, "updated_at": None, "items": []}

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "indices": out_indices,
        "feeds": out_feeds,
    }

    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    live_idx = sum(1 for v in out_indices.values() if v.get("ok"))
    live_feed = sum(1 for v in out_feeds.values() if v.get("ok"))
    print(f"Done. {live_idx}/{len(INDICES)} indices live, {live_feed}/{len(FEEDS)} feeds live.")


if __name__ == "__main__":
    main()
