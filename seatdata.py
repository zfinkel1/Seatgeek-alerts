"""
SeatData client — what a performer's tickets ACTUALLY sold for.

This is the piece that answers the question everything else kept deferring:
not "is this person famous" but "do their tickets move, and at what price."

WHY IT WORKS EVEN THOUGH IT DOESN'T KNOW OUR EVENTS
SeatData has never heard of the niche events we find -- Cooper Union returns
zero, and Andy Cohen's current tour isn't in it at all. That's the point: if the
event were already in a resale dataset we'd be late. We use it for the
PERFORMER's history instead, which is what actually predicts resale:

    Andy Cohen, 7 past events, 208 sales, median $211, 50-77 tickets per night

VOLUME MATTERS AS MUCH AS PRICE
A single comedy-club sale at $65 against a $33 face looks like a 2x. But one
sale is not a market -- you cannot put real money through it. Every result here
reports tickets-per-event alongside price, because the difference between Andy
Cohen (50-77/night) and a comedy club (0-1/night) is the difference between a
trade you can size and one you can't.

RATE LIMITS (from /v1/account)
  events_search   6/min   <- the bottleneck; everything else is generous
  salesdata     500/min
  listings      500/min
  events_stats  300/min

Search is FREE; only sales pulls cost. So match aggressively, pull selectively.

    export SEATDATA_API_KEY=...
    python seatdata.py "Andy Cohen" "Marlon Wayans"
"""
import os
import sys
import json
import time
import statistics
import urllib.parse
import urllib.request

API_KEY = os.environ.get("SEATDATA_API_KEY")
BASE = os.environ.get("SEATDATA_BASE", "https://seatdata.io/api")

SEARCH_INTERVAL = 11.0      # 6/min, with headroom
_last_search = [0.0]


def _req(path, body=None, timeout=90):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    # Bearer, not the raw key: /account happens to accept both but every other
    # endpoint returns "missing_api_key" without the prefix.
    req.add_header("Authorization", "Bearer " + (API_KEY or ""))
    req.add_header("Accept", "application/json")
    req.add_header("User-Agent", "Mozilla/5.0")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            return {"error": json.loads(e.read().decode("utf-8", "replace")),
                    "status": e.code}
        except Exception:
            return {"error": str(e.reason), "status": e.code}
    except Exception as e:
        return {"error": str(e)[:150], "status": 0}


def account():
    return _req("/v1/account")


def usage():
    return _req("/v1/usage")


def search_events(**params):
    """Free event lookup. Self-throttled to respect the 6/min ceiling."""
    wait = SEARCH_INTERVAL - (time.time() - _last_search[0])
    if wait > 0:
        time.sleep(wait)
    _last_search[0] = time.time()
    q = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    d = _req("/v1/events/search?" + q)
    return (d.get("data") or []) if isinstance(d, dict) else []


def sales_batch(event_ids):
    """Sales for up to 100 events per request. Returns {event_id: [sales]}."""
    out = {}
    ids = list(event_ids)
    for i in range(0, len(ids), 100):
        d = _req("/v0.3/salesdata/batch", {"event_ids": ids[i:i + 100]})
        if isinstance(d, dict) and "results" in d:
            for k, v in d["results"].items():
                out[int(k)] = v or []
        elif isinstance(d, dict) and d.get("error"):
            print(f"  [seatdata] batch error: {json.dumps(d)[:140]}")
    return out


def performer_value(name, max_events=100):
    """What this performer's tickets sell for, from real transactions.

    Past events only -- an upcoming event has no sales yet, and including it
    would drag the average toward zero for no reason."""
    events = search_events(event_name=name, historical="true", limit=max_events)
    past = [e for e in events if (e.get("event_date") or "") < time.strftime("%Y-%m-%d")]
    if not past:
        return {"performer": name, "events": len(events), "past_events": 0,
                "sales": 0, "median": None, "per_event": 0, "verdict": "no history"}

    sales_by_event = sales_batch([e["event_id"] for e in past])
    prices, qty, evs_with_sales = [], 0, 0
    venues = {}
    for eid, rows in sales_by_event.items():
        if not rows:
            continue
        evs_with_sales += 1
        prices += [r["price"] for r in rows if r.get("price")]
        qty += sum(r.get("quantity") or 0 for r in rows)
        ev = next((e for e in past if e["event_id"] == eid), {})
        venues[ev.get("venue_name") or "?"] = len(rows)

    if not prices:
        return {"performer": name, "events": len(events), "past_events": len(past),
                "sales": 0, "median": None, "per_event": 0,
                "verdict": "tracked but never resold"}

    prices.sort()
    per_event = round(qty / evs_with_sales, 1) if evs_with_sales else 0
    # Liquidity is the gate, not the price. A 2x on one ticket a night is not a
    # business; it's an anecdote you can't size.
    verdict = ("liquid" if per_event >= 20 else
               "thin" if per_event >= 5 else "illiquid")
    return {
        "performer": name,
        "events": len(events),
        "past_events": len(past),
        "events_with_sales": evs_with_sales,
        "sales": len(prices),
        "tickets": qty,
        "per_event": per_event,
        "median": round(statistics.median(prices), 2),
        "low": round(prices[0], 2),
        "high": round(prices[-1], 2),
        "p25": round(prices[len(prices) // 4], 2),
        "venues": sorted(venues.items(), key=lambda kv: -kv[1])[:3],
        "verdict": verdict,
    }


def rank(names, on_result=None):
    out = []
    for i, n in enumerate(names, 1):
        r = performer_value(n)
        out.append(r)
        if on_result:
            on_result(i, len(names), r)
    out.sort(key=lambda r: (r.get("per_event") or 0, r.get("median") or 0),
             reverse=True)
    return out


def format_table(rows):
    lines = [f"{'performer':<26}{'sold':>6}{'tix/evt':>9}{'median':>9}"
             f"{'p25':>8}{'verdict':>11}",
             "-" * 69]
    for r in rows:
        if not r.get("median"):
            lines.append(f"{r['performer'][:25]:<26}{'—':>6}{'—':>9}{'—':>9}"
                         f"{'—':>8}{r['verdict']:>11}")
            continue
        lines.append(f"{r['performer'][:25]:<26}{r['sales']:>6}{r['per_event']:>9}"
                     f"{r['median']:>9.0f}{r['p25']:>8.0f}{r['verdict']:>11}")
    return "\n".join(lines)


if __name__ == "__main__":
    if not API_KEY:
        print("ERROR: SEATDATA_API_KEY not set")
        raise SystemExit(1)
    names = sys.argv[1:] or ["Andy Cohen"]
    print(f"[seatdata] valuing {len(names)} performer(s)\n")
    rows = rank(names, on_result=lambda i, n, r: print(
        f"  {i}/{n} {r['performer'][:28]:<30}"
        f"{r.get('sales', 0):>5} sales  {r['verdict']}"))
    print()
    print(format_table(rows))
