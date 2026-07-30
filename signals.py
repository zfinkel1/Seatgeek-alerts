"""
Market signals over TicketGenie time-series data.

Pure functions over the API's documented shapes — no network, no keys — so the
math can be verified before we spend a dollar of trial credit. Run this file
directly to execute the self-test.

WHY THESE SIGNALS AND NOT CHART PATTERNS
Candlestick/RSI/MACD assume a continuous, liquid, two-sided series. A ticket event
has none of that: a few dozen days of history, no bid side, and a dominant
deterministic drift (price decays into a hard expiry because sellers must clear).
Run a pattern detector on that and it mostly rediscovers the decay curve.

So every signal here is one of three kinds:
  1. DECAY-ADJUSTED — measured as a residual against what this event's cohort
     normally does at the same days-to-event. Signal lives in the residual.
  2. FLOW — derived from confirmed sales (the only true demand observation in an
     ask-only market, since there is no bid side to read).
  3. LADDER STRUCTURE — shape of the ask stack, which is a real one-sided order
     book: depth, dispersion, and where it is thinning.

SPEC (SHORT) INVENTORY — the correction that matters
Brokers list tickets they do not own and cover later. Two consequences:
  - The visible ask ladder OVERSTATES real supply. Listing counts alone are not
    float; a post-onsale count spike is often spec, not inventory.
  - Spec sellers must cover before the event or eat the marketplace penalty. That
    is a hard-deadline forced buyer, which is exactly the mechanism of a genuine
    short squeeze. Thin real inventory + heavy spec = the setup worth hunting.
"""
import math
from collections import defaultdict

# All-in take rate lost on the way out (founder's actual broker rate, 2026-07-30).
# Note this is well below watch.py's legacy 15% default -- trades the old flip
# engine rejected as too thin may clear at this rate.
FEE_PCT = 0.08


# ---------------------------------------------------------------- flow (sales)

def vwap(sales):
    """Volume-weighted average of CONFIRMED sales — the honest "price".

    watch.py currently prices an event by its cheapest ask, which in equities
    would be quoting a stock by its lowest resting limit order. A cheap ask is
    an offer nobody took; a sale is a price someone actually paid."""
    num = den = 0.0
    for s in sales:
        p, q = s.get("atp"), s.get("quantity") or 0
        if p is None or q <= 0:
            continue
        num += float(p) * q
        den += q
    return (num / den) if den else None


def absorption(sales, days):
    """Tickets clearing per day over the trailing window. The denominator of
    days-of-supply, and the closest thing to a demand rate we can observe."""
    if days <= 0:
        return 0.0
    return sum((s.get("quantity") or 0) for s in sales) / float(days)


def sales_acceleration(early, late, days_each):
    """Change in clearing rate between two equal windows. Positive = demand
    building. This is the second derivative that separates an event waking up
    from one merely ticking along."""
    a0 = absorption(early, days_each)
    a1 = absorption(late, days_each)
    if a0 <= 0:
        return None if a1 <= 0 else float("inf")
    return (a1 - a0) / a0


# ------------------------------------------------------------ ladder structure

def ladder(listings):
    """Shape of the one-sided book. `p10` is the cheap tier — it gets eaten first
    in a real demand event, so it moves before the headline get-in price does."""
    prices = sorted(float(L["price"]) for L in listings
                    if L.get("price") is not None and L.get("available", True))
    if not prices:
        return None
    n = len(prices)

    def pct(q):
        if n == 1:
            return prices[0]
        i = q * (n - 1)
        lo = int(math.floor(i))
        hi = min(lo + 1, n - 1)
        return prices[lo] + (prices[hi] - prices[lo]) * (i - lo)

    p50 = pct(0.50)
    return {
        "count": n,
        "qty": sum(int(L.get("quantity") or 0) for L in listings),
        "min": prices[0],
        "p10": pct(0.10),
        "p50": p50,
        "p90": pct(0.90),
        # Dispersion as a fraction of the median: sellers disagreeing. A widening
        # spread often precedes a move, and a very wide one means the "going
        # rate" is unreliable — the same thing watch.py's FLIP_DEPTH gate guards.
        "dispersion": (pct(0.90) - pct(0.10)) / p50 if p50 else None,
    }


def erosion(prev, curr):
    """Cheap-tier depletion relative to the whole ladder.

    Positive = the bottom is lifting faster than the middle, i.e. buyers are
    eating the cheap seats. This leads the headline price, which is why it is
    worth computing separately rather than just watching `min`."""
    if not prev or not curr or not prev.get("p10") or not prev.get("p50"):
        return None
    d10 = (curr["p10"] - prev["p10"]) / prev["p10"]
    d50 = (curr["p50"] - prev["p50"]) / prev["p50"]
    return d10 - d50


# --------------------------------------------------- listing-level attribution

def attribute_departures(prev_listings, curr_listings, sales_between, tol=0.12):
    """Split vanished listings into SOLD vs PULLED, and count repricing.

    This is the signal the old scraper could never compute: a falling listing
    count is ambiguous — sold (bullish) or delisted (bearish) look identical in
    aggregate counts. Stable listing `id`s plus the confirmed-sales feed
    disambiguate: a listing that disappears with a matching sale (same section,
    price within `tol`) is attributed SOLD; otherwise PULLED.

    Repricing is the other half, and it has no equity analog worth the name: a
    seller RAISING an ask is volunteering that they expect more demand. The share
    of listings repriced up is a genuine forward-looking sentiment measure."""
    prev_by_id = {L["id"]: L for L in prev_listings if L.get("id")}
    curr_by_id = {L["id"]: L for L in curr_listings if L.get("id")}

    # Index sales by section so matching is cheap and section-aware.
    by_section = defaultdict(list)
    for s in sales_between:
        by_section[str(s.get("section") or "").strip().lower()].append(s)

    def matches_a_sale(L):
        sec = str(L.get("section") or "").strip().lower()
        price = float(L.get("price") or 0)
        for s in by_section.get(sec, ()):
            sp = s.get("atp")
            if sp and price and abs(float(sp) - price) / price <= tol:
                return True
        return False

    sold = pulled = 0
    for lid, L in prev_by_id.items():
        if lid in curr_by_id:
            continue
        if matches_a_sale(L):
            sold += 1
        else:
            pulled += 1

    up = down = 0
    for lid, L in curr_by_id.items():
        was = prev_by_id.get(lid)
        if not was or was.get("price") is None or L.get("price") is None:
            continue
        if float(L["price"]) > float(was["price"]) * 1.005:
            up += 1
        elif float(L["price"]) < float(was["price"]) * 0.995:
            down += 1

    repriced = up + down
    survivors = len(set(prev_by_id) & set(curr_by_id))
    return {
        "sold": sold,
        "pulled": pulled,
        "new": len(set(curr_by_id) - set(prev_by_id)),
        "repriced_up": up,
        "repriced_down": down,
        # Share of repricing that was upward. None when nobody repriced — an
        # honest "no information", not a misleading 0.5.
        "reprice_up_share": (up / repriced) if repriced else None,
        "survivors": survivors,
        # Of listings that left, how many actually cleared. Low sell-through on a
        # shrinking book means sellers are walking away, not buyers arriving.
        "sell_through": (sold / (sold + pulled)) if (sold + pulled) else None,
    }


# ------------------------------------------------------------- float & squeeze

def float_state(primary_count, secondary_count, spec_estimate=0):
    """Tradeable float, discounted for spec (short) listings that aren't real
    inventory. `spec_estimate` is a count, not a rate — supply it from whatever
    heuristic you trust (e.g. secondary listings that appeared without matching
    primary depletion)."""
    real_secondary = max(0, (secondary_count or 0) - (spec_estimate or 0))
    return {
        "primary": primary_count or 0,
        "secondary": secondary_count or 0,
        "spec_estimate": spec_estimate or 0,
        "real_float": (primary_count or 0) + real_secondary,
    }


def days_of_supply(real_float, absorption_per_day):
    """How long current inventory lasts at the current clearing rate."""
    if not absorption_per_day or absorption_per_day <= 0:
        return float("inf")
    return real_float / absorption_per_day


def squeeze_score(*, days_to_event, real_float, absorption_per_day,
                  primary_delta, price_trend, sell_through,
                  reprice_up_share, erosion_score, spec_share=0.0):
    """Composite 0..1 squeeze reading, plus the reasons behind it.

    HARD GATES first — these are disqualifying regardless of everything else:
      * primary inventory RISING. The issuer is still releasing holds, so any
        shortage can be erased overnight. This is the lesson from the Jul-28
        chart: primary went 1,280 -> 1,470 while secondary counts climbed.
      * price trending DOWN while the book shrinks. That is capitulation
        (sellers leaving), not accumulation (buyers arriving).
      * event already passed.

    Then a weighted blend of the continuous evidence."""
    reasons = []
    if days_to_event is not None and days_to_event <= 0:
        return 0.0, ["event has passed"]
    if primary_delta is not None and primary_delta > 0:
        return 0.0, [f"primary inventory rising (+{primary_delta}): issuer still releasing"]
    if price_trend is not None and price_trend < 0:
        return 0.0, [f"price trending down ({price_trend:+.1%}): capitulation, not demand"]

    dos = days_of_supply(real_float, absorption_per_day)
    # Runs out before the event -> shortage forming. Ratio, capped so a
    # near-zero-supply event can't dominate the blend on its own.
    pressure = 0.0 if dos == float("inf") else min(days_to_event / dos, 3.0) / 3.0
    if pressure > 0.33:
        reasons.append(f"supply lasts {dos:.0f}d vs {days_to_event:.0f}d to event")

    components = [(pressure, 0.35)]
    if sell_through is not None:
        components.append((sell_through, 0.20))
        if sell_through > 0.6:
            reasons.append(f"{sell_through:.0%} of departures were real sales")
    if reprice_up_share is not None:
        components.append((reprice_up_share, 0.20))
        if reprice_up_share > 0.6:
            reasons.append(f"{reprice_up_share:.0%} of repricing was upward")
    if price_trend is not None:
        components.append((min(max(price_trend / 0.15, 0.0), 1.0), 0.15))
    if erosion_score is not None:
        components.append((min(max(erosion_score / 0.10, 0.0), 1.0), 0.10))

    total_w = sum(w for _, w in components)
    score = sum(v * w for v, w in components) / total_w if total_w else 0.0

    # Spec-heavy books are unreliable: the visible ladder isn't real inventory.
    # Haircut rather than gate, since some spec is normal and it cuts both ways
    # (spec sellers are also the forced buyers that make a squeeze violent).
    if spec_share:
        score *= (1.0 - min(spec_share, 0.5))
        reasons.append(f"discounted for ~{spec_share:.0%} estimated spec inventory")
    return round(min(max(score, 0.0), 1.0), 3), reasons


def net_edge(buy_price, expected_resale, fee_pct=FEE_PCT):
    """Profit after the marketplace haircut. Any signal has to clear this before
    it means anything — a 15% round trip erases most 'significant' edges."""
    if not buy_price:
        return None
    return (expected_resale * (1 - fee_pct) - buy_price) / buy_price


# ------------------------------------------------------------------- self-test

def _selftest():
    print("signals.py self-test\n" + "-" * 60)

    # Scenario A — SQUEEZE: book shrinking because seats are SELLING, cheap tier
    # eroding, sellers marking up, primary exhausted.
    prev_a = [{"id": f"a{i}", "price": 100 + i * 10, "section": "120",
               "quantity": 2, "available": True} for i in range(12)]
    curr_a = ([{"id": f"a{i}", "price": 100 + i * 10, "section": "120",
                "quantity": 2, "available": True} for i in range(4, 12)]
              + [{"id": "a5", "price": 175, "section": "120", "quantity": 2,
                  "available": True}])
    curr_a = {L["id"]: L for L in curr_a}
    curr_a["a5"]["price"] = 175          # seller marked UP from 150
    curr_a["a6"]["price"] = 185          # and another
    curr_a = list(curr_a.values())
    sales_a = [{"atp": 100 + i * 10, "quantity": 2, "section": "120"} for i in range(4)]

    attr_a = attribute_departures(prev_a, curr_a, sales_a)
    lad_prev, lad_curr = ladder(prev_a), ladder(curr_a)
    score_a, why_a = squeeze_score(
        days_to_event=21, real_float=lad_curr["qty"],
        absorption_per_day=absorption(sales_a, 3),
        primary_delta=0, price_trend=0.12,
        sell_through=attr_a["sell_through"],
        reprice_up_share=attr_a["reprice_up_share"],
        erosion_score=erosion(lad_prev, lad_curr),
    )
    print(f"A/squeeze   attribution={attr_a}")
    print(f"            score={score_a}  reasons={why_a}")

    # Scenario B — DECAY: same shrinkage, but listings were PULLED (no matching
    # sales) and prices marked DOWN. Must be gated to zero.
    sales_b = []
    attr_b = attribute_departures(prev_a, curr_a, sales_b)
    score_b, why_b = squeeze_score(
        days_to_event=21, real_float=lad_curr["qty"],
        absorption_per_day=absorption(sales_b, 3),
        primary_delta=0, price_trend=-0.08,
        sell_through=attr_b["sell_through"],
        reprice_up_share=attr_b["reprice_up_share"],
        erosion_score=0.0,
    )
    print(f"B/decay     attribution={attr_b}")
    print(f"            score={score_b}  reasons={why_b}")

    # Scenario C — the Jul-28 chart: supply BUILDING, primary released. Gated.
    score_c, why_c = squeeze_score(
        days_to_event=60, real_float=1400, absorption_per_day=5,
        primary_delta=+190, price_trend=0.02, sell_through=0.5,
        reprice_up_share=0.5, erosion_score=0.0,
    )
    print(f"C/onsale    score={score_c}  reasons={why_c}")

    print("-" * 60)
    checks = [
        ("A scores high", score_a > 0.55),
        ("B gated to zero", score_b == 0.0),
        ("C gated to zero", score_c == 0.0),
        ("A > B", score_a > score_b),
        ("sold/pulled split correct", attr_a["sold"] == 4 and attr_b["pulled"] == 4),
        ("upward repricing detected", attr_a["repriced_up"] == 2),
        ("vwap is volume-weighted", abs(vwap(sales_a) - 115.0) < 1e-9),
        # buy 100, resell 150, lose FEE_PCT on the way out.
        ("net_edge nets the fee",
         abs(net_edge(100, 150) - (150 * (1 - FEE_PCT) - 100) / 100) < 1e-9),
        ("fee is the founder's 8%, not the legacy 15%", abs(FEE_PCT - 0.08) < 1e-9),
    ]
    ok = True
    for name, passed in checks:
        print(f"  [{'PASS' if passed else 'FAIL'}] {name}")
        ok &= passed
    print("-" * 60)
    print("ALL PASS" if ok else "FAILURES ABOVE")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_selftest())
