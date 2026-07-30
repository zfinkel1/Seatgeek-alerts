"""
Pairing job: face price vs secondary price, recorded per event.

THE QUESTION IT ANSWERS
"What will this ticket sell for?" -- asked as "what do events like this clear at,
relative to face?" The MULTIPLE is the thing worth modelling, not the price:

  * face is known exactly (you're the one buying it), so half the equation has
    no error in it,
  * multiples pool across performers and venues, so a few hundred observations
    beat a deep history on one artist,
  * and they move far less than absolute prices do.

Every event where we can see BOTH a primary face price and a secondary price is
one observation. Collect them and the model builds itself. Nobody else is
assembling this because nobody else is reading primary and secondary together.

HONEST LIMIT
SeatGeek's public API returns ASK statistics, not sold prices. Asks are
aspirational -- measured on real TicketGenie data, one event's median ask was $795
against $259 actually paid. So `multiple_ask` here is an UPPER BOUND on what you
would realise. Sold data (DataIQ) replaces this input without changing the method;
until then treat the recorded multiple as optimistic and mark it as such.

MIRRORS
An event whose secondary listings mirror primary inventory one-for-one is not
evidence of demand -- it is a competitor listing tickets they don't own. Those
rows are flagged `suspect_mirror` and must be excluded when fitting the model.
"""
import json
import os
import re
import statistics
import urllib.parse
import urllib.request
from datetime import datetime, timedelta

CLIENT_ID = os.environ.get("SEATGEEK_PUBLIC_CLIENT_ID", "MTY2MnwxMzgzMzIwMTU4")
PAIRS_FILE = os.path.join(
    os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "pairs.jsonl")

# Blend used to turn ask statistics into an expected clearing price. Mirrors
# primary.ASK_BLEND -- the single most important parameter to recalibrate once
# real sold data exists.
ASK_BLEND = 0.4


def _sg(path, **params):
    params["client_id"] = CLIENT_ID
    url = f"https://api.seatgeek.com/2/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _token_overlap(a, b):
    ta, tb = set(_norm(a).split()), set(_norm(b).split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def find_secondary(name, date=None, venue=None, window_days=1):
    """Locate a primary event on SeatGeek and return its secondary stats.

    DATE is the strong key. Two acts share a name far more often than two events
    share a name AND a date, so the date window does most of the matching work
    and the name similarity only breaks ties. Without a date this is unreliable
    and we say so via `confidence`."""
    params = {"q": name, "per_page": 25}
    if date:
        lo = (date - timedelta(days=window_days)).strftime("%Y-%m-%d")
        hi = (date + timedelta(days=window_days)).strftime("%Y-%m-%d")
        params["datetime_local.gte"] = lo
        params["datetime_local.lte"] = hi
    try:
        d = _sg("events", **params)
    except Exception as e:
        return {"matched": False, "error": str(e)[:120]}

    best, best_score = None, 0.0
    for e in d.get("events", []):
        score = _token_overlap(name, e.get("title") or "")
        for p in e.get("performers") or []:
            score = max(score, _token_overlap(name, p.get("name") or ""))
        if venue:
            score += 0.3 * _token_overlap(venue, (e.get("venue") or {}).get("name"))
        if score > best_score:
            best, best_score = e, score

    if not best or best_score < 0.3:
        return {"matched": False, "candidates": len(d.get("events", []))}

    st = best.get("stats") or {}
    v = best.get("venue") or {}
    low, avg = st.get("lowest_price"), st.get("average_price")
    clearing = None
    if avg is not None:
        clearing = (low + ASK_BLEND * (avg - low)) if low is not None \
            else avg * ASK_BLEND
    return {
        "matched": True,
        "confidence": "high" if (date and best_score >= 0.6) else
                      ("medium" if date else "low"),
        "sg_event_id": best.get("id"),
        "sg_title": best.get("title"),
        "sg_url": best.get("url"),
        "sg_date": (best.get("datetime_local") or "")[:10],
        "sg_venue": v.get("name"),
        "sg_capacity": v.get("capacity"),
        "low": low,
        "avg_ask": avg,
        "listing_count": st.get("listing_count"),
        "est_clearing": clearing,
        "match_score": round(best_score, 3),
    }


def detect_mirror(primary_seat_labels, listing_count):
    """Crude first-pass mirror flag.

    A real signal needs seat-level secondary data (which SeatGeek's public API
    doesn't give), so for now the tell is a secondary listing count suspiciously
    close to primary availability -- consistent with someone listing inventory
    they haven't bought. Deliberately conservative: this FLAGS for exclusion, it
    does not conclude. Upgrade once listing-level secondary data is available."""
    n_primary = len(primary_seat_labels or [])
    if not n_primary or not listing_count:
        return False, None
    ratio = listing_count / float(n_primary)
    return (0.8 <= ratio <= 1.25), round(ratio, 3)


def pair(event_name, face_price, *, date=None, venue=None, category=None,
         source=None, url=None, primary_seats=None, currency="$"):
    """Build one observation. Returns the record (also appended to PAIRS_FILE)."""
    sec = find_secondary(event_name, date=date, venue=venue)
    rec = {
        "recorded_at": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "name": event_name,
        "date": date.isoformat() if date else None,
        "venue": venue,
        "source": source,
        "url": url,
        "currency": currency,
        "face": face_price,
        "band": category,
        "primary_seats": len(primary_seats or []) or None,
        **{k: v for k, v in sec.items() if k != "error"},
    }
    if sec.get("matched") and face_price:
        est = sec.get("est_clearing")
        rec["multiple_ask"] = round(sec["avg_ask"] / face_price, 3) \
            if sec.get("avg_ask") else None
        # The number to model. Optimistic until sold data replaces the ask blend.
        rec["multiple_est"] = round(est / face_price, 3) if est else None
        suspect, ratio = detect_mirror(primary_seats, sec.get("listing_count"))
        rec["suspect_mirror"] = suspect
        rec["mirror_ratio"] = ratio
    try:
        with open(PAIRS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception as e:
        print(f"  [pairing] could not append: {e}")
    return rec


def load_pairs(path=None):
    path = path or PAIRS_FILE
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    return out


def multiples(pairs=None, min_n=3):
    """Fitted multiples by venue capacity tier -- the model, such as it is.

    Mirror-suspect rows are excluded: a listing nobody bought is not evidence.
    Reports n alongside every figure so a two-observation 'model' is visibly
    a two-observation model."""
    pairs = pairs if pairs is not None else load_pairs()
    usable = [p for p in pairs
              if p.get("multiple_est") and not p.get("suspect_mirror")]
    buckets = {}
    for p in usable:
        cap = p.get("sg_capacity") or 0
        tier = ("intimate" if cap and cap <= 400 else
                "small" if cap and cap <= 1200 else
                "mid" if cap and cap <= 3000 else
                "large" if cap else "unknown")
        buckets.setdefault(tier, []).append(p["multiple_est"])
    out = {}
    for tier, vals in buckets.items():
        if len(vals) < min_n:
            out[tier] = {"n": len(vals), "median": None, "note": "too few"}
        else:
            out[tier] = {"n": len(vals),
                         "median": round(statistics.median(vals), 3),
                         "low": round(min(vals), 3),
                         "high": round(max(vals), 3)}
    return {"observations": len(pairs), "usable": len(usable),
            "excluded_mirrors": sum(1 for p in pairs if p.get("suspect_mirror")),
            "by_tier": out}


def _selftest():
    assert _token_overlap("Andy Cohen", "Andy Cohen") == 1.0
    assert _token_overlap("Andy Cohen", "Naomi Klein") == 0.0
    assert 0 < _token_overlap("Naomi Klein: End Times", "Naomi Klein") < 1
    s, r = detect_mirror(["a"] * 100, 100)
    assert s and r == 1.0, (s, r)
    s2, _ = detect_mirror(["a"] * 100, 12)
    assert not s2
    assert detect_mirror([], 50) == (False, None)
    fake = [{"multiple_est": 1.4, "sg_capacity": 900},
            {"multiple_est": 1.6, "sg_capacity": 900},
            {"multiple_est": 1.5, "sg_capacity": 900},
            {"multiple_est": 9.0, "sg_capacity": 900, "suspect_mirror": True}]
    m = multiples(fake)
    assert m["by_tier"]["small"]["n"] == 3, m
    assert m["by_tier"]["small"]["median"] == 1.5, m
    assert m["excluded_mirrors"] == 1
    print("pairing.py self-test: ALL PASS\n")


if __name__ == "__main__":
    _selftest()
    from datetime import date as _d
    tests = [
        ("Andy Cohen", 75.0, _d(2026, 10, 20), "The Great Hall at Cooper Union"),
        ("Malcolm Gladwell", 60.0, _d(2026, 10, 15), "Lincoln Theatre"),
        ("Naomi Klein", 49.95, _d(2026, 9, 30), "Central Hall Westminster"),
    ]
    print(f"{'event':<20}{'face':>7}{'sec.avg':>9}{'est':>8}{'mult':>7}  conf")
    print("-" * 62)
    for name, face, dt, venue in tests:
        r = pair(name, face, date=dt, venue=venue, source="demo")
        if not r.get("matched"):
            print(f"{name:<20}{face:>7.0f}{'no match':>9}{'-':>8}{'-':>7}  -")
            continue
        print(f"{name:<20}{face:>7.0f}{(r.get('avg_ask') or 0):>9.0f}"
              f"{(r.get('est_clearing') or 0):>8.0f}"
              f"{(r.get('multiple_est') or 0):>7.2f}  {r.get('confidence')}")
    print()
    print(json.dumps(multiples(), indent=1))
