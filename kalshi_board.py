#!/usr/bin/env python3
"""
Kalshi Futures Board - collector.

Pulls current prices for NFL / NBA / NHL futures and award markets from Kalshi's
PUBLIC market-data API (no API key needed), appends an hourly snapshot to the
history files, computes 1h / 24h / 7d moves, and writes data/latest.json for the
dashboard.

Usage:
    python collector/kalshi_board.py                 # hourly snapshot (what the scheduler runs)
    python collector/kalshi_board.py --backfill 30   # also backfill N days from Kalshi candlesticks
    python collector/kalshi_board.py --list-series   # print which series were matched and why

Standard library only.
"""
import argparse
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
HIST = os.path.join(DATA, "history")
CONFIG_PATH = os.path.join(ROOT, "collector", "config.json")

API_BASES = [
    "https://api.elections.kalshi.com/trade-api/v2",
    "https://external-api.kalshi.com/trade-api/v2",
]

HOUR = 3600
DAY = 86400


# --------------------------------------------------------------------------- utils

def now_ts():
    # FAKE_NOW lets the test harness simulate a sequence of hourly runs.
    fake = os.environ.get("FAKE_NOW")
    return int(fake) if fake else int(time.time())


def load_json(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, obj, compact=True):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        if compact:
            json.dump(obj, f, separators=(",", ":"), ensure_ascii=False)
        else:
            json.dump(obj, f, indent=1, ensure_ascii=False)
    os.replace(tmp, path)


def prob_to_american(p):
    """Implied probability (0-1) -> American odds (int). None outside (0,1)."""
    if p is None or p <= 0 or p >= 1:
        return None
    if p >= 0.5:
        return -round(100 * p / (1 - p))
    return round(100 * (1 - p) / p)


def price(m, key):
    """Read a Kalshi price as dollars (0-1). Handles new *_dollars strings and legacy cents."""
    v = m.get(key + "_dollars")
    if v not in (None, ""):
        try:
            return float(v)
        except (TypeError, ValueError):
            pass
    v = m.get(key)
    if v not in (None, ""):
        try:
            return float(v) / 100.0
        except (TypeError, ValueError):
            pass
    return None


def num(m, key):
    v = m.get(key + "_fp", m.get(key))
    try:
        return float(v) if v not in (None, "") else None
    except (TypeError, ValueError):
        return None


def mid_price(bid, ask, last, max_spread):
    """Fair-value price for charting: bid/ask mid when the book is tight, else last trade."""
    bid = bid if bid and bid > 0 else None
    ask = ask if ask and ask < 1 else None
    if bid is not None and ask is not None and ask - bid <= max_spread:
        return (bid + ask) / 2
    if last and 0 < last < 1:
        # clamp last trade inside the current book if a book exists
        if bid is not None and last < bid:
            return bid
        if ask is not None and last > ask:
            return ask
        return last
    if bid is not None and ask is not None:
        return (bid + ask) / 2
    return bid if bid is not None else ask


# --------------------------------------------------------------------------- HTTP

class Kalshi:
    def __init__(self, pause=0.08):
        self.pause = pause
        self.base = None
        self.calls = 0

    def get(self, path, params=None, tries=5):
        bases = [self.base] if self.base else API_BASES
        last_err = None
        for base in bases:
            url = base + path
            if params:
                url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
            for attempt in range(tries):
                try:
                    req = urllib.request.Request(url, headers={
                        "Accept": "application/json",
                        "User-Agent": "kalshi-futures-board/1.0",
                    })
                    with urllib.request.urlopen(req, timeout=30) as r:
                        body = json.loads(r.read().decode("utf-8"))
                    self.base = base
                    self.calls += 1
                    time.sleep(self.pause)
                    return body
                except urllib.error.HTTPError as e:
                    last_err = e
                    if e.code == 429 or e.code >= 500:
                        time.sleep(min(2 ** attempt, 20))
                        continue
                    if e.code == 404:
                        return None
                    break
                except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                    last_err = e
                    time.sleep(min(2 ** attempt, 20))
        raise RuntimeError(f"Kalshi request failed: {path} {params} -> {last_err}")

    def paged(self, path, params, key):
        out, cursor = [], None
        for _ in range(100):
            p = dict(params)
            if cursor:
                p["cursor"] = cursor
            body = self.get(path, p) or {}
            out.extend(body.get(key) or [])
            cursor = body.get("cursor")
            if not cursor:
                break
        return out


# --------------------------------------------------------------------------- discovery

SPORT_RULES = {
    "NFL": {
        "prefixes": ("KXNFL", "KXSB", "KXAFC", "KXNFC"),
        "words": ("pro football", "nfl", "super bowl", "afc ", "nfc "),
        "not": ("college", "ncaa", "cfb", "canadian", "cfl", "ufl", "xfl"),
    },
    "NBA": {
        "prefixes": ("KXNBA",),
        "words": ("pro basketball", "nba"),
        "not": ("wnba", "college", "ncaa", "euroleague", "g league"),
    },
    "NHL": {
        "prefixes": ("KXNHL", "KXSTANLEY"),
        "words": ("pro hockey", "nhl", "stanley cup"),
        "not": ("college", "ncaa", "khl", "ahl", "pwhl"),
    },
}

AWARD_WORDS = ("mvp", "most valuable", "of the year", "rookie", "defensive player",
               "offensive player", "comeback", "sixth man", "most improved", "clutch",
               "award", "trophy", "hart", "vezina", "norris", "calder", "selke",
               "conn smythe", "art ross", "rocket richard", "jack adams", "lady byng",
               "dpoy", "opoy", "droy", "oroy", "roy")


def classify_sport(ticker, text):
    t = ticker.upper()
    for sport, r in SPORT_RULES.items():
        if any(n in text for n in r["not"]):
            continue
        if t.startswith(r["prefixes"]) or any(w in text for w in r["words"]):
            return sport
    return None


def classify_kind(text):
    if any(w in text for w in AWARD_WORDS):
        return "Award"
    if "division" in text:
        return "Division"
    if any(w in text for w in ("conference", "afc", "nfc", "eastern", "western")):
        return "Conference"
    if any(w in text for w in ("playoff", "postseason")):
        return "Playoffs"
    if any(w in text for w in ("champion", "super bowl", "stanley cup", "finals", "title")):
        return "Champion"
    return "Other"


def series_text(s):
    tags = " ".join(s.get("tags") or [])
    return f" {s.get('title', '')} {tags} ".lower()


def discover_series(api, cfg):
    """Return [{ticker,title,sport,kind,reason}] for futures/award series we should track."""
    all_series = api.paged("/series", {"category": "Sports"}, "series")
    if not all_series:  # some API versions ignore/omit category filter
        all_series = [s for s in api.paged("/series", {}, "series")
                      if (s.get("category") or "").lower() == "sports"]

    inc_set = {x.upper() for x in cfg.get("include_series", [])}
    exc_set = {x.upper() for x in cfg.get("exclude_series", [])}
    inc_kw = [k.lower() for k in cfg.get("include_keywords", [])]
    exc_kw = [k.lower() for k in cfg.get("exclude_keywords", [])]
    sports = set(cfg.get("sports", ["NFL", "NBA", "NHL"]))
    kinds = set(cfg.get("kinds", []))

    matched, rejected = [], []
    for s in all_series:
        tk = (s.get("ticker") or "").upper()
        text = series_text(s)
        sport = classify_sport(tk, text)
        kind = classify_kind(text)
        row = {"ticker": tk, "title": s.get("title", ""), "sport": sport, "kind": kind}
        if tk in exc_set:
            rejected.append({**row, "reason": "exclude_series"}); continue
        if tk in inc_set:
            matched.append({**row, "reason": "include_series"}); continue
        if sport not in sports:
            continue
        hit_ex = next((k for k in exc_kw if k in text), None)
        if hit_ex:
            rejected.append({**row, "reason": f"exclude keyword '{hit_ex.strip()}'"}); continue
        hit_in = next((k for k in inc_kw if k in text), None)
        if not hit_in:
            rejected.append({**row, "reason": "no futures/award keyword"}); continue
        if kinds and kind not in kinds:
            rejected.append({**row, "reason": f"kind {kind} not enabled"}); continue
        matched.append({**row, "reason": f"keyword '{hit_in.strip()}'"})
    return matched, rejected


# --------------------------------------------------------------------------- snapshot

def fetch_markets(api, series, cfg):
    """Pull open events + nested markets for each matched series."""
    max_spread = cfg.get("max_spread_for_mid", 0.10)
    fee = cfg.get("kalshi_fee_rate", 0.07)
    rows = []
    for s in series:
        events = api.paged("/events", {
            "series_ticker": s["ticker"], "status": "open",
            "with_nested_markets": "true", "limit": 200,
        }, "events")
        for ev in events:
            ev_title = ev.get("title") or s["title"]
            ev_sub = ev.get("sub_title") or ""
            mkts = ev.get("markets") or []
            if not mkts:
                body = api.get("/markets", {"event_ticker": ev.get("event_ticker"), "limit": 1000}) or {}
                mkts = body.get("markets") or []
            for m in mkts:
                if (m.get("status") or "active") not in ("active", "open"):
                    continue
                bid, ask, last = price(m, "yes_bid"), price(m, "yes_ask"), price(m, "last_price")
                mid = mid_price(bid, ask, last, max_spread)
                if mid is None or mid <= 0:
                    continue
                strike = m.get("custom_strike") or {}
                name = (m.get("yes_sub_title") or m.get("subtitle")
                        or (next(iter(strike.values()), None) if isinstance(strike, dict) else None)
                        or m.get("title") or m["ticker"])
                if str(name).strip().lower() in ("yes", "") or name == m["ticker"]:
                    name = ev_sub or ev_title or m["ticker"]
                # Single-outcome events (e.g. "Will the Bears make the playoffs?") are grouped
                # by series so the board shows one card with every team, not 32 one-row cards.
                single = len(mkts) == 1 and not ev.get("mutually_exclusive")
                grp, grpt = (s["ticker"], s["title"]) if single else (ev.get("event_ticker"), None)
                buy = ask if ask and 0 < ask < 1 else None
                rows.append({
                    "t": m["ticker"],
                    "s": s["ticker"],
                    "ev": ev.get("event_ticker"),
                    "evt": ev_title + (f" - {ev_sub}" if ev_sub and ev_sub.lower() not in ev_title.lower() else ""),
                    "mx": bool(ev.get("mutually_exclusive")),
                    "g": grp,
                    "gt": grpt,
                    "sport": s["sport"],
                    "kind": s["kind"],
                    "name": str(name),
                    "bid": round(bid, 4) if bid else None,
                    "ask": round(ask, 4) if ask else None,
                    "last": round(last, 4) if last else None,
                    "p": round(mid, 4),
                    "buyfee": round(buy + fee * buy * (1 - buy), 4) if buy else None,
                    "vol24": num(m, "volume_24h"),
                    "oi": num(m, "open_interest"),
                    "close": m.get("close_time") or m.get("expected_expiration_time"),
                })
    return rows


def add_fair(rows):
    """No-vig probability: normalize mids within each mutually-exclusive event."""
    by_ev = {}
    for r in rows:
        by_ev.setdefault(r["ev"], []).append(r)
    for ev, rs in by_ev.items():
        tot = sum(r["p"] for r in rs)
        ok = rs[0]["mx"] and len(rs) >= 2 and tot > 0
        for r in rs:
            r["fair"] = round(r["p"] / tot, 4) if ok else None
            r["evsum"] = round(tot, 4) if ok else None


# --------------------------------------------------------------------------- history

def hist_path(series_ticker):
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", series_ticker)
    return os.path.join(HIST, f"{safe}.json")


def compact(points, now, hourly_days):
    """Keep hourly points for `hourly_days`; older points thinned to one per UTC day (last)."""
    cutoff = now - hourly_days * DAY
    old, recent = [], []
    for p in points:
        (old if p[0] < cutoff else recent).append(p)
    daily = {}
    for p in old:
        daily[p[0] // DAY] = p
    return sorted(daily.values()) + recent


def merge_points(existing, new_pts, min_gap=20 * 60):
    """Merge [ts,val] lists; a new point replaces any existing point within min_gap seconds."""
    pts = sorted(existing)
    for np_ in sorted(new_pts):
        pts = [p for p in pts if abs(p[0] - np_[0]) >= min_gap]
        pts.append(np_)
        pts.sort()
    return pts


def value_at(points, ts, tolerance):
    """Last value at or before ts, if it is no older than ts - tolerance."""
    best = None
    for p in points:
        if p[0] <= ts:
            best = p
        else:
            break
    if best and ts - best[0] <= tolerance:
        return best[1]
    return None


def update_history(rows, now, cfg):
    hourly_days = cfg.get("history_hourly_days", 21)
    by_series = {}
    for r in rows:
        by_series.setdefault(r["s"], []).append(r)
    histories = {}
    for s, rs in by_series.items():
        path = hist_path(s)
        h = load_json(path, {})
        for r in rs:
            pts = h.get(r["t"], [])
            pts = merge_points(pts, [[now, round(r["p"] * 100, 2)]])
            h[r["t"]] = compact(pts, now, hourly_days)
        save_json(path, h)
        histories[s] = h
    return histories


def add_moves(rows, histories, now, cfg):
    windows = cfg.get("move_windows_hours", [1, 24, 168])
    for r in rows:
        pts = histories.get(r["s"], {}).get(r["t"], [])
        cur = r["p"] * 100
        r["d"] = {}
        r["prev"] = {}
        for w in windows:
            target = now - w * HOUR
            # allow a bit of slop: scheduled runs drift; long windows use daily points
            tol = max(int(w * HOUR * 0.25), 45 * 60) if w <= 24 else DAY + 2 * HOUR
            v = value_at(pts, target + 10 * 60, tol)
            r["d"][str(w)] = round(cur - v, 2) if v is not None else None
            r["prev"][str(w)] = v
        # 7-day sparkline, one point every 6h
        spark = []
        for k in range(28, -1, -1):
            v = value_at(pts, now - k * 6 * HOUR + 10 * 60, 6 * HOUR)
            spark.append(v)
        r["spark"] = spark


# --------------------------------------------------------------------------- backfill

def backfill(api, rows, days, cfg, now):
    """Fill history from Kalshi hourly candlesticks for markets currently on the board."""
    max_spread = cfg.get("max_spread_for_mid", 0.10)
    start = now - days * DAY
    by_series = {}
    for r in rows:
        by_series.setdefault(r["s"], []).append(r)
    done = 0
    for s, rs in by_series.items():
        path = hist_path(s)
        h = load_json(path, {})
        for r in rs:
            # Hourly candles for the last 21 days, daily before that (Kalshi caps candles per call)
            pts = []
            spans = []
            hourly_from = max(start, now - cfg.get("history_hourly_days", 21) * DAY)
            if start < hourly_from:
                spans.append((start, hourly_from, 1440))
            spans.append((hourly_from, now, 60))
            for a, b, interval in spans:
                body = api.get(f"/series/{s}/markets/{r['t']}/candlesticks", {
                    "start_ts": a, "end_ts": b, "period_interval": interval,
                }) or {}
                for c in body.get("candlesticks") or []:
                    ts = c.get("end_period_ts")
                    bid = price(c.get("yes_bid") or {}, "close")
                    ask = price(c.get("yes_ask") or {}, "close")
                    last = price(c.get("price") or {}, "close")
                    mid = mid_price(bid, ask, last, max_spread)
                    if ts and mid:
                        pts.append([int(ts), round(mid * 100, 2)])
            existing = h.get(r["t"], [])
            # keep live snapshots; candles fill gaps only
            have = [p[0] for p in existing]
            fill = [p for p in pts if all(abs(p[0] - t) >= 40 * 60 for t in have)]
            h[r["t"]] = compact(sorted(existing + fill), now, cfg.get("history_hourly_days", 21))
            done += 1
        save_json(path, h)
    print(f"backfilled {done} markets")


# --------------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", type=int, default=0, help="days of candlestick history to backfill")
    ap.add_argument("--list-series", action="store_true")
    args = ap.parse_args()

    cfg = load_json(CONFIG_PATH, {})
    os.makedirs(HIST, exist_ok=True)
    api = Kalshi(pause=cfg.get("request_pause_seconds", 0.08))
    now = now_ts()

    series, rejected = discover_series(api, cfg)
    save_json(os.path.join(DATA, "catalog.json"), {
        "updated": now, "matched": series, "rejected": rejected,
    }, compact=False)
    if args.list_series:
        for s in series:
            print(f"+ {s['sport']:3} {s['kind']:10} {s['ticker']:24} {s['title']}  ({s['reason']})")
        for s in rejected:
            if s["sport"]:
                print(f"- {s['sport']:3} {s['kind']:10} {s['ticker']:24} {s['title']}  ({s['reason']})")
        return

    if not series:
        print("No series matched - check collector/config.json", file=sys.stderr)
        sys.exit(1)

    rows = fetch_markets(api, series, cfg)
    if not rows:
        print("No open markets returned - leaving previous data in place", file=sys.stderr)
        sys.exit(1)
    add_fair(rows)

    if args.backfill:
        backfill(api, rows, args.backfill, cfg, now)

    histories = update_history(rows, now, cfg)
    add_moves(rows, histories, now, cfg)

    for r in rows:
        r["am"] = prob_to_american(r["p"])
        r["amfair"] = prob_to_american(r["fair"]) if r.get("fair") else None
        r["ambuy"] = prob_to_american(r["buyfee"]) if r.get("buyfee") else None

    save_json(os.path.join(DATA, "latest.json"), {
        "updated": now,
        "windows": cfg.get("move_windows_hours", [1, 24, 168]),
        "fee_rate": cfg.get("kalshi_fee_rate", 0.07),
        "series": series,
        "markets": rows,
    })
    print(f"{datetime.fromtimestamp(now, timezone.utc):%Y-%m-%d %H:%M}Z  "
          f"{len(series)} series, {len(rows)} markets, {api.calls} API calls")


if __name__ == "__main__":
    main()
