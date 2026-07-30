"""
Primary inventory depletion tracking — the cleanest squeeze signal available.

WHY PRIMARY BEATS SECONDARY FOR THIS
Every secondary signal we've built has to fight ambiguity. A listing that vanishes
was sold OR pulled and the counts look identical. Mirrors inflate apparent supply
with tickets nobody owns. Asks run far above what buyers pay -- one measured event
showed a $795 median ask against $259 actually paid.

Primary has none of that. A seat that leaves the chart is sold. Full stop. So
sell-through measured here is a real measurement rather than an inference, which
makes it the highest-quality demand signal in the system -- and it's free.

It is also the CEILING. While the box office still has seats at face, resale
can't run. Primary selling out is the moment that constraint lifts, which is why
"about to sell out" is worth an alert on its own.

THE ASSUMPTION THIS RESTS ON
That seats.io's `forSale` object list SHRINKS as seats sell, rather than being a
static list of seats the operator released. One snapshot cannot tell you which.
`verify_assumption()` checks it across two real snapshots -- run it before
trusting any number here. If the list turns out to be static, the whole approach
needs per-seat status instead, and the signal is worth chasing that far.

STORAGE
Append-only JSONL per snapshot, same pattern as history.py. Depletion is only
visible over time, so the first run establishes a baseline and tells you nothing.
"""
import json
import os
from datetime import datetime, timezone, date

import seatsio

SNAP_FILE = os.path.join(
    os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "primary_snapshots.jsonl")

# Fraction of the run-up by which we expect a healthy event to have sold out.
# An event on pace to exhaust well before its date is the squeeze case.
SELLOUT_URGENCY = 0.75


def snapshot(page_url, event_date=None, label=None, path=None):
    """Record one observation of an event's primary inventory."""
    inv = seatsio.inventory(page_url=page_url)
    now = datetime.now(timezone.utc)
    rec = {
        "t": now.isoformat(timespec="seconds"),
        "url": page_url,
        "label": label,
        "event_date": event_date.isoformat() if event_date else None,
        "event_key": inv["event_key"],
        "total": inv["total"],
        "currency": inv.get("currency"),
        # Seat labels are stored so the DIFF can name exactly which seats sold --
        # that's what makes it a measurement rather than a count.
        "labels": [s["label"] for s in inv["seats"]],
        "by_category": {k: len(v) for k, v in inv["by_category"].items()},
        "prices": {k: (v[0].get("price") if v else None)
                   for k, v in inv["by_category"].items()},
    }
    try:
        with open(path or SNAP_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"  [depletion] append failed: {e}")
    return rec


def load(path=None, url=None):
    path = path or SNAP_FILE
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
            except Exception:
                continue
            if url is None or r.get("url") == url:
                out.append(r)
    out.sort(key=lambda r: r["t"])
    return out


def _parse(ts):
    return datetime.fromisoformat(ts)


def diff(a, b):
    """What sold between two snapshots, by seat label and by category."""
    la, lb = set(a.get("labels") or []), set(b.get("labels") or [])
    sold = la - lb
    added = lb - la          # operator releasing more inventory -- see below
    hours = max((_parse(b["t"]) - _parse(a["t"])).total_seconds() / 3600.0, 1e-6)
    per_cat = {}
    for k, n in (a.get("by_category") or {}).items():
        per_cat[k] = n - (b.get("by_category") or {}).get(k, 0)
    return {
        "from": a["t"], "to": b["t"], "hours": round(hours, 2),
        "sold": len(sold), "sold_labels": sorted(sold)[:40],
        # New seats appearing means a HOLD RELEASE. Same lesson as the Jul-28
        # Ticketmaster chart: the issuer can refill the pool at will, so a
        # shortage can evaporate overnight. Never treat this as demand.
        "released": len(added),
        "sold_per_day": round(len(sold) / (hours / 24.0), 2),
        "by_category": {k: v for k, v in per_cat.items() if v},
        "remaining": b["total"],
    }


def analyse(snaps, event_date=None):
    """Depletion state from a series of snapshots. Needs at least two."""
    if len(snaps) < 2:
        return {"ok": False, "reason": "need >= 2 snapshots; first run is a baseline"}
    first, last = snaps[0], snaps[-1]
    ed = event_date
    if ed is None and last.get("event_date"):
        ed = date.fromisoformat(last["event_date"])

    d = diff(first, last)
    # Recent pace, using the last pair, so a burst near the event isn't diluted
    # by weeks of flat early sales.
    recent = diff(snaps[-2], last)
    pace = recent["sold_per_day"] or d["sold_per_day"]
    remaining = last["total"]
    started = first["total"]
    sold_total = max(started - remaining, 0)

    days_left = (ed - datetime.now(timezone.utc).date()).days if ed else None
    days_of_supply = (remaining / pace) if pace > 0 else None
    projected_sellout = (days_of_supply is not None
                         and days_left is not None
                         and days_of_supply < days_left * SELLOUT_URGENCY)

    return {
        "ok": True,
        "started_with": started,
        "remaining": remaining,
        "sold_total": sold_total,
        "pct_sold": round(sold_total / started, 3) if started else None,
        "pace_per_day": pace,
        "released_since_start": d["released"],
        "days_to_event": days_left,
        "days_of_supply": round(days_of_supply, 1) if days_of_supply else None,
        "projected_sellout": bool(projected_sellout),
        "sold_out": remaining == 0,
        "by_category_sold": d["by_category"],
    }


def alert_reason(state):
    """Whether this warrants a message, and what it should say.

    Gates first, because they override everything:
      * inventory being RELEASED means the operator is still feeding the market
      * no sales at all means the pace number is meaningless
    """
    if not state.get("ok"):
        return None
    if state["sold_out"]:
        return ("PRIMARY SOLD OUT — face-value ceiling is gone; "
                "secondary is now free to run")
    if state.get("released_since_start", 0) > 0 and not state["projected_sellout"]:
        return None       # operator still releasing holds; any shortage is soft
    if state["pace_per_day"] <= 0:
        return None
    if state["projected_sellout"]:
        return (f"SQUEEZE FORMING — {state['remaining']} seats left, selling "
                f"{state['pace_per_day']:.0f}/day = {state['days_of_supply']:.0f} "
                f"days of supply vs {state['days_to_event']} days to event")
    if state.get("pct_sold") and state["pct_sold"] >= 0.8:
        return f"{state['pct_sold']:.0%} of inventory gone — watch for sellout"
    return None


def verify_assumption(page_url, path=None):
    """Does the forSale list actually shrink as seats sell?

    Everything here depends on it. Compares the two most recent snapshots for
    this URL and reports what it sees rather than assuming."""
    snaps = load(path, url=page_url)
    if len(snaps) < 2:
        return {"verified": None,
                "reason": "need two snapshots taken at different times"}
    d = diff(snaps[-2], snaps[-1])
    if d["sold"] == 0 and d["released"] == 0:
        return {"verified": None, "reason": "inventory unchanged — inconclusive",
                **d}
    return {"verified": True,
            "note": "list changes over time, so it tracks real availability",
            **d}


def _selftest():
    a = {"t": "2026-07-01T00:00:00+00:00", "total": 100,
         "labels": [f"A-A-{i}" for i in range(100)],
         "by_category": {"Band A": 100}}
    b = {"t": "2026-07-11T00:00:00+00:00", "total": 60,
         "labels": [f"A-A-{i}" for i in range(40, 100)],
         "by_category": {"Band A": 60}}
    d = diff(a, b)
    assert d["sold"] == 40 and d["released"] == 0, d
    assert d["sold_per_day"] == 4.0, d

    st = analyse([a, b], event_date=date(2026, 7, 31))
    assert st["sold_total"] == 40 and st["remaining"] == 60
    assert st["pace_per_day"] == 4.0
    assert st["days_of_supply"] == 15.0, st

    # Hold release must suppress a squeeze call.
    c = {"t": "2026-07-11T00:00:00+00:00", "total": 140,
         "labels": [f"A-A-{i}" for i in range(140)],
         "by_category": {"Band A": 140}}
    st2 = analyse([a, c], event_date=date(2026, 7, 31))
    assert st2["released_since_start"] == 40, st2
    assert alert_reason(st2) is None, "release must gate the alert"

    sold_out = {"t": "2026-07-11T00:00:00+00:00", "total": 0, "labels": [],
                "by_category": {}}
    st3 = analyse([a, sold_out], event_date=date(2026, 7, 31))
    assert st3["sold_out"] and "SOLD OUT" in alert_reason(st3)
    assert analyse([a])["ok"] is False
    print("depletion.py self-test: ALL PASS")


if __name__ == "__main__":
    _selftest()
