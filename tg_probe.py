"""
TicketGenie API reconnaissance — run this FIRST, before building anything on top.

Three answers decide whether the squeeze/backtest work is viable, and none of them
are in the docs:

  1. HISTORY DEPTH. Every time-series response carries `availableDateRange`. A
     backtest needs months; the dashboard screenshot only showed ~2 days. If depth
     is measured in days, the whole "backtest before risking capital" plan is dead
     and we'd be back to collecting our own history going forward.
  2. MARKETPLACE PERMISSIONS. Keys carry per-marketplace grants. Ticketmaster
     PRIMARY listings (`TMPrimaryListings`) are the price ceiling in the squeeze
     model — without them the float is only half visible. A denied marketplace
     returns 403 INSUFFICIENT_PERMISSIONS on listing endpoints.
  3. COST SHAPE. Billing is per-CALL by pricing class and scope, NOT per row
     returned. That inverts normal API tuning: fetch the largest page you're
     allowed and batch aggressively, because 20 small calls cost 20x one big one.

Usage:
    $env:TICKETGENIE_API_KEY = "tg_ak_..."      # PowerShell
    export TICKETGENIE_API_KEY=tg_ak_...        # bash
    python tg_probe.py [event search term]

Reads the key from the environment only — never hardcode it, and never commit it.
"""
import os
import sys
import json
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("TICKETGENIE_BASE", "https://api.ticketgenie.io/api/v1")
API_KEY = os.environ.get("TICKETGENIE_API_KEY")

# Marketplaces the key may or may not be granted. TM primary/secondary are split
# because they're separate permissions and only TM exposes the primary/secondary
# distinction the float model needs.
PLATFORMS = ["ticketmaster", "stubhub", "seatgeek", "gametime", "axs"]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def call(method, path, body=None, timeout=60):
    """One API call. Returns (status, parsed_json_or_text). Never raises on HTTP
    error — a 403 is a RESULT here (it tells us a permission is missing), not a
    failure, so we report it rather than blowing up the probe."""
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"ApiKey {API_KEY}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            payload = e.reason
        return e.code, payload
    except Exception as e:
        return 0, str(e)


def section(title):
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def probe_pricing():
    """Rate card. Tells us which endpoints are cheap enough to call in a loop and
    which need batching — the difference between a $20 backtest and a $2,000 one."""
    section("1. RATE CARD  (GET /usage-pricing)")
    status, body = call("GET", "/usage-pricing")
    if status != 200:
        print(f"  ! {status}: {body}")
        return
    unit = body.get("unit", "micros")
    print(f"  billing model: {body.get('model')}  unit: {unit}  "
          f"currency: {body.get('currencyCode')}")
    print(f"  charged on: {body.get('chargeableStatuses')}")
    rows = sorted(body.get("endpoints", []), key=lambda e: e.get("priceMicros") or 0)
    print(f"\n  {'price($)':>10}  {'scope':<12} {'window':>7}  endpoint")
    for e in rows:
        usd = (e.get("priceMicros") or 0) / 1_000_000
        win = e.get("windowHours")
        print(f"  {usd:>10.4f}  {str(e.get('scope') or ''):<12} "
              f"{(str(win) + 'h') if win else '':>7}  "
              f"{e.get('method')} {e.get('path')}")


def probe_search(term):
    """Find one real event to probe. Also the first read of `periodSummary`, which
    silently OMITS marketplaces the key can't see — a free permissions hint."""
    section(f"2. EVENT SEARCH  (POST /events)  query={term!r}")
    status, body = call("POST", "/events", {
        "search": {"query": term},
        "meta": {"limit": 5},
    })
    if status != 200:
        print(f"  ! {status}: {json.dumps(body)[:400]}")
        return None
    events = body.get("events") or body.get("data") or body.get("results") or []
    if not events:
        print(f"  no events matched. raw keys: {list(body)[:10]}")
        print(f"  {json.dumps(body)[:600]}")
        return None
    print(f"  {len(events)} event(s); showing structure of the first:\n")
    first = events[0]
    print(json.dumps(first, indent=1)[:1800])
    return first


def _native_id(event, prefer=("ticketmaster", "seatgeek", "stubhub")):
    """Pull a usable {id, platform} out of whatever shape search returned.

    Search returns `nativeRefs`: one entry per marketplace carrying that
    marketplace's own event id under `nativeId`. Prefer Ticketmaster (it's the
    only platform exposing the primary/secondary split), then SeatGeek (where we
    actually buy), before falling back to anything present."""
    refs = []
    for key in ("nativeRefs", "nativeIds", "platformEvents", "sources"):
        v = event.get(key)
        if isinstance(v, dict):
            v = [v]
        if isinstance(v, list):
            for item in v:
                if not isinstance(item, dict):
                    continue
                nid = item.get("nativeId") or item.get("id")
                plat = item.get("platform")
                if nid and plat:
                    refs.append({"id": str(nid), "platform": plat})
    if not refs:
        return None
    for plat in prefer:
        for r in refs:
            if r["platform"] == plat:
                return r
    return refs[0]


def _all_native_ids(event):
    """Every marketplace ref on an event — history depth can differ per platform."""
    out = []
    for item in event.get("nativeRefs") or []:
        nid, plat = item.get("nativeId"), item.get("platform")
        if nid and plat:
            out.append({"id": str(nid), "platform": plat})
    return out


def probe_history(native):
    """THE decisive question: how far back does data actually go?

    `availableDateRange` is authoritative — it's what the vendor will actually
    serve, regardless of what the plan page advertises. We ask for a deliberately
    huge window (2 years) so the response has to tell us where it truncates."""
    section(f"3. HISTORY DEPTH  ({native['platform']} / {native['id']})")
    now = datetime.now(timezone.utc)
    wide = {
        "from": (now - timedelta(days=730)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    print("\n  -- listing snapshots (the ask ladder over time) --")
    status, body = call("POST", "/events/listings/timeseries/snapshots", {
        "nativeId": native, "dateRange": wide, "interval": "1h",
        "sort": {"field": "TIMESTAMP", "order": "ASC"},
        "page": {"limit": 1000},
    })
    if status != 200:
        print(f"  ! {status}: {json.dumps(body)[:300]}")
    else:
        rng = body.get("availableDateRange") or {}
        snaps = body.get("snapshots") or []
        print(f"     availableDateRange: {rng.get('start')} -> {rng.get('end')}")
        print(f"     totalSnapshots: {body.get('totalSnapshots')}  (returned {len(snaps)})")
        if len(snaps) >= 2:
            # Real cadence, not the requested interval — tells us the finest
            # resolution actually stored behind the normalization parameter.
            gaps = []
            for a, b in zip(snaps, snaps[1:]):
                try:
                    ta = datetime.fromisoformat(a["timestamp"].replace("Z", "+00:00"))
                    tb = datetime.fromisoformat(b["timestamp"].replace("Z", "+00:00"))
                    gaps.append((tb - ta).total_seconds() / 60)
                except Exception:
                    pass
            if gaps:
                gaps.sort()
                print(f"     snapshot gap (min/median/max): {gaps[0]:.0f} / "
                      f"{gaps[len(gaps)//2]:.0f} / {gaps[-1]:.0f} minutes")
            print(f"     first: {snaps[0]}")
            print(f"     last:  {snaps[-1]}")

    print("\n  -- confirmed sales (ground truth for a backtest) --")
    status, body = call("POST", "/events/sales/timeseries", {
        "nativeId": native, "dateRange": wide,
        "sort": {"field": "DATE", "order": "ASC"},
        "page": {"limit": 1000},
    })
    if status != 200:
        print(f"  ! {status}: {json.dumps(body)[:300]}")
    else:
        rng = body.get("availableDateRange") or {}
        sales = body.get("sales") or []
        print(f"     availableDateRange: {rng.get('start')} -> {rng.get('end')}")
        print(f"     totalCount: {body.get('totalCount')}  (returned {len(sales)})")
        for s in sales[:3]:
            print(f"     e.g. {s}")


def probe_permissions(native):
    """Which marketplaces can this key actually read? TM primary is the one that
    matters most — it's the price ceiling in the squeeze model."""
    section("4. MARKETPLACE PERMISSIONS")
    now = datetime.now(timezone.utc)
    window = {
        "from": (now - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "to": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    print("\n  -- Ticketmaster primary vs secondary split --")
    for inv in ("PRIMARY", "SECONDARY"):
        status, body = call("POST", "/events/listings/timeseries/snapshots", {
            "nativeId": {"id": native["id"], "platform": "ticketmaster"},
            "dateRange": window, "interval": "1h",
            "inventoryType": inv, "page": {"limit": 1},
        })
        if status == 200:
            n = body.get("totalSnapshots")
            note = "" if n else "  (granted, but no data for this event)"
            print(f"     {inv:<10} OK — {n} snapshots{note}")
        elif status == 403:
            print(f"     {inv:<10} DENIED (403) — permission not on this key")
        else:
            print(f"     {inv:<10} {status}: {json.dumps(body)[:160]}")

    print("\n  -- per-marketplace current listings --")
    for plat in PLATFORMS:
        status, body = call("POST", "/events/listings/current", {
            "nativeId": {"id": native["id"], "platform": plat},
            "page": {"limit": 1},
        })
        label = {200: "OK", 403: "DENIED (403)"}.get(status, str(status))
        detail = "" if status in (200, 403) else f" {json.dumps(body)[:120]}"
        print(f"     {plat:<14} {label}{detail}")


def probe_seatmap(native):
    """Section capacities = the denominator the current flip engine lacks. With
    capacity we can compute PERCENT SOLD per section instead of raw counts, and
    learn real zones from co-movement instead of guessing +-3 section numbers."""
    section("5. SEATMAP / SECTION CAPACITIES")
    path = f"/events/{native['platform']}/{native['id']}/seatmap/sections"
    status, body = call("GET", path)
    if status != 200:
        print(f"  ! {status}: {json.dumps(body)[:300]}")
        return
    print(json.dumps(body, indent=1)[:1500])


def main():
    if not API_KEY:
        print(__doc__)
        print("ERROR: TICKETGENIE_API_KEY is not set.")
        sys.exit(1)
    term = " ".join(sys.argv[1:]) or "Cubs"
    print(f"[tg_probe] base={BASE}  key=...{API_KEY[-6:]}")

    probe_pricing()
    event = probe_search(term)
    if not event:
        print("\n[tg_probe] no event to probe — pass a search term, e.g. "
              "`python tg_probe.py Morgan Wallen`")
        return
    native = _native_id(event)
    if not native:
        print("\n[tg_probe] could not find a nativeId in the search result above.")
        print("           Copy one in manually and re-run the history probe.")
        return
    others = _all_native_ids(event)
    if others:
        print("\n[tg_probe] marketplaces on this event: "
              + ", ".join(f"{r['platform']}:{r['id']}" for r in others))
    probe_history(native)
    probe_permissions(native)
    probe_seatmap(native)
    print("\n[tg_probe] done — the availableDateRange values in section 3 are the "
          "go/no-go for backtesting.")


if __name__ == "__main__":
    main()
