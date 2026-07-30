"""
Scoring a niche-primary buy: what will it resell for, and is the spread real?

THE PROBLEM
You're looking at an event with NO secondary market yet -- nobody has listed it,
so there is no price to read. You have to predict one before you buy.

THE METHOD: COMPARABLES
A performer carries their price with them. Andy Cohen's other dates tell you what
Andy Cohen sells for; the venue tells you how that price shifts. Real numbers from
SeatGeek's free API:

    Bergen PAC (1,367 seats)        avg $179   218 listings
    Cooper Union Great Hall (~900)  avg $297    31 listings

Same performer, same month. The smaller room prints a HIGHER price on FEWER
listings. Scarcity is the dominant term, and it is observable.

THE TRAP: YOUR OWN VOLUME
"Purchase limit doesn't matter, I can buy a ton" cuts both ways. Dump 50 tickets
into a market carrying 31 listings and you ARE the market -- the $297 average will
not survive your own selling. Every score here is penalised by how much of the
book you'd become. This is the single most likely way a great-looking niche buy
turns into a mediocre one.

Data source is SeatGeek's public API -- free, no vendor, no anti-bot. The same
client_id already used by scrape.py.
"""
import json
import statistics
import urllib.parse
import urllib.request

CLIENT_ID = "MTY2MnwxMzgzMzIwMTU4"
FEE_PCT = 0.08

# Venue capacity buckets. Absolute capacity is often unknown, so we work in
# coarse tiers -- precision here is false comfort anyway.
TIERS = [
    (400,    "intimate"),
    (1200,   "small theatre"),
    (3000,   "theatre"),
    (8000,   "large"),
    (10**9,  "arena"),
]


def tier_of(capacity):
    if not capacity:
        return None
    for cap, name in TIERS:
        if capacity <= cap:
            return name
    return "arena"


def sg(path, **params):
    params["client_id"] = CLIENT_ID
    url = f"https://api.seatgeek.com/2/{path}?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def performer_events(name, per_page=50):
    """Every upcoming SeatGeek event for a performer, with secondary stats."""
    d = sg("events", q=name, per_page=per_page)
    out = []
    for e in d.get("events", []):
        st = e.get("stats") or {}
        v = e.get("venue") or {}
        out.append({
            "date": (e.get("datetime_local") or "")[:10],
            "venue": v.get("name"),
            "city": (v.get("city") or ""),
            "capacity": v.get("capacity"),
            "low": st.get("lowest_price"),
            "avg": st.get("average_price"),
            "listings": st.get("listing_count") or 0,
        })
    return out


# SeatGeek's stats.average_price is the average of LISTED ASKS, not of sales.
# We measured the gap directly on TicketGenie data for one event: median ask $795
# against $259 actually paid. Asks are aspirational; using them raw would have the
# model predicting resale prices no buyer ever pays.
#
# So expected clearing price is modelled as sitting between the get-in and the
# average ask, nearer the bottom: low + BLEND * (avg - low). 0.4 is a deliberately
# conservative starting guess and is THE parameter to calibrate first in Phase 2,
# once real buy/sell results exist. Until then every margin here is a hypothesis.
ASK_BLEND = 0.4


def expected_clearing(low, avg):
    """Estimated price a buyer actually pays, from SeatGeek's ask statistics."""
    if avg is None:
        return None
    if low is None:
        return avg * ASK_BLEND
    return low + ASK_BLEND * (avg - low)


def baseline(events):
    """What this performer is worth on secondary, from their observed dates.

    Uses the MEDIAN of per-event clearing estimates: a single sold-out or dead
    date shouldn't set the baseline. Returns None without enough priced
    comparables -- an honest 'we don't know' beats a number built on one point."""
    priced = []
    for e in events:
        c = expected_clearing(e.get("low"), e.get("avg"))
        if c:
            priced.append((c, e))
    if len(priced) < 2:
        return None
    vals = sorted(c for c, _ in priced)
    return {
        "n": len(priced),
        "median_avg": statistics.median(vals),
        "low": min(vals),
        "high": max(vals),
        "median_listings": statistics.median(
            [e["listings"] for _, e in priced]) or 0,
    }


def predict_price(base, capacity=None, comparable_capacity=None):
    """Expected secondary average for a NEW date by this performer.

    Scarcity adjustment: a room half the size of the comparable prints a higher
    price. Modelled as a damped inverse ratio -- (comp/cap) ** 0.35 -- rather
    than straight inverse, because price does not double when a room halves.
    The exponent is a starting guess to be calibrated once Phase 2 has real
    buy/sell results; it is deliberately conservative.

    Returns (predicted_price, confidence) where confidence is 'low'|'medium'|'high'.
    """
    if not base:
        return None, "none"
    price = base["median_avg"]
    if capacity and comparable_capacity:
        ratio = comparable_capacity / float(capacity)
        price *= max(0.5, min(ratio ** 0.35, 2.0))   # clamp: no wild extrapolation
    conf = "high" if base["n"] >= 5 else ("medium" if base["n"] >= 3 else "low")
    return price, conf


def volume_impact(quantity, existing_listings, elasticity=0.6):
    """How much your own selling depresses the price.

    You become `quantity` of a book that will hold `existing + quantity`. The
    haircut scales with the share of the book you represent. elasticity=0.6 means
    becoming half the market costs you ~30% of the price -- aggressive enough to
    stop the model from pretending you can dump unlimited volume into a thin
    event, which is exactly the failure mode of a 'buy a ton' strategy.
    """
    if quantity <= 0:
        return 0.0
    share = quantity / float(existing_listings + quantity)
    return min(share * elasticity, 0.6)


def score(face_price, predicted, listings, quantity, fee_pct=FEE_PCT):
    """Net margin on a niche-primary buy, after fees AND your own price impact.

    Returns a dict with the numbers a buy decision actually needs."""
    if not (face_price and predicted):
        return None
    impact = volume_impact(quantity, listings)
    realised = predicted * (1 - impact)
    net = realised * (1 - fee_pct)
    profit = net - face_price
    return {
        "face": face_price,
        "predicted": round(predicted, 2),
        "volume_impact": round(impact, 3),
        "realised": round(realised, 2),
        "net_after_fee": round(net, 2),
        "profit_per_ticket": round(profit, 2),
        "margin": round(profit / face_price, 3),
        "total_profit": round(profit * quantity, 2),
    }


def _demo():
    print("primary.py — scoring demo on live SeatGeek data\n" + "=" * 66)
    for name in ("Andy Cohen", "Malcolm Gladwell"):
        evs = performer_events(name)
        b = baseline(evs)
        print(f"\n{name}")
        for e in evs[:5]:
            print(f"   {e['date']}  {str(e['venue'])[:34]:<34} "
                  f"avg={e['avg']}  listings={e['listings']}")
        if not b:
            print("   not enough priced comparables -> no baseline")
            continue
        print(f"   baseline: median avg ${b['median_avg']:.0f} "
              f"(range ${b['low']:.0f}-${b['high']:.0f}, n={b['n']}, "
              f"median listings {b['median_listings']:.0f})")

        # A hypothetical niche date: 400-seat room, comparable was ~1,200.
        pred, conf = predict_price(b, capacity=400, comparable_capacity=1200)
        print(f"   predicted for a 400-seat room: ${pred:.0f}  (confidence {conf})")

        face = 65.0
        for q in (10, 50, 200):
            s = score(face, pred, listings=int(b["median_listings"]), quantity=q)
            print(f"     buy {q:>3} @ ${face:.0f}  impact {s['volume_impact']:.0%}"
                  f"  net ${s['net_after_fee']:.0f}/tix"
                  f"  margin {s['margin']:+.0%}"
                  f"  total ${s['total_profit']:,.0f}")
    print("\n" + "=" * 66)
    print("Note how margin decays with size: the same trade at 200 tickets is a")
    print("different trade than at 10. That is the constraint on 'buy a ton'.")


def _selftest():
    assert tier_of(300) == "intimate" and tier_of(20000) == "arena"
    # asks blend toward the get-in, not the average
    assert expected_clearing(100, 300) == 100 + 0.4 * 200
    assert expected_clearing(None, None) is None
    b = {"n": 4, "median_avg": 200.0, "low": 150, "high": 300, "median_listings": 40}
    p, c = predict_price(b, capacity=400, comparable_capacity=1200)
    assert p > 200 and c == "medium", (p, c)          # smaller room -> higher price
    p2, _ = predict_price(b, capacity=2400, comparable_capacity=1200)
    assert p2 < 200, p2                                # bigger room -> lower price
    assert volume_impact(0, 50) == 0.0
    assert volume_impact(50, 50) > volume_impact(5, 50)
    s_small = score(65, 250, listings=40, quantity=10)
    s_big = score(65, 250, listings=40, quantity=200)
    assert s_big["margin"] < s_small["margin"], "volume must hurt margin"
    assert baseline([{"avg": 100, "listings": 5}]) is None, "1 comp is not a baseline"
    print("primary.py self-test: ALL PASS\n")


if __name__ == "__main__":
    _selftest()
    _demo()
