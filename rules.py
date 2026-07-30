"""
Candidate buying rules, written as testable hypotheses.

Each rule is a guess about what makes a ticket go up. None of them are known to
work. The point of writing them down like this is that a rule you can name and
argue about is a rule you can test -- and running them all against the same events
tells you which guesses were right.

Two of these are CONTROLS, not strategies:
  - buy_everything: the market's own drift. Any real rule has to beat it, because
    "my rule made 8%" means nothing if buying blindly made 12%.
  - buy_nothing: sanity check that the harness reports an empty result cleanly.

Usage:
    from backtest import run
    from rules import RULES, compare
    print(compare(events, decide_days_before=30, horizon_days=14))
"""
from backtest import run, format_report


def _listing_trend(w, days=7):
    """Fractional change in how many tickets are listed. Negative = the book is
    shrinking, which is the supply side of a squeeze."""
    if len(w.snapshots) < 2:
        return None
    cutoff = w.t - days * 86400.0
    past = [s for s in w.snapshots if s["t"] <= cutoff] or [w.snapshots[0]]
    then = len(past[-1].get("listings", ()))
    now = w.listing_count()
    if not then:
        return None
    return (now - then) / then


def _primary_count(w):
    """Listings flagged as primary in the latest snapshot. None when the source
    doesn't distinguish (only Ticketmaster does), so rules can skip rather than
    silently treat 'unknown' as 'sold out'."""
    snap = w.latest
    if not snap:
        return None
    listings = snap.get("listings", ())
    if not any("primary" in L for L in listings):
        return None
    return sum(1 for L in listings if L.get("primary"))


def _cheap_tier_trend(w, days=7, q=0.10):
    """Movement in the bottom of the ask ladder vs the middle. Positive means the
    cheap seats are being eaten faster than the rest -- usually the earliest
    visible sign of real demand, before the headline price moves."""
    if len(w.snapshots) < 2:
        return None
    cutoff = w.t - days * 86400.0
    past = [s for s in w.snapshots if s["t"] <= cutoff] or [w.snapshots[0]]

    def pcts(snap):
        ps = sorted(float(L["price"]) for L in snap.get("listings", ())
                    if L.get("price") is not None)
        if len(ps) < 3:
            return None, None
        lo = ps[max(0, int(q * (len(ps) - 1)))]
        mid = ps[len(ps) // 2]
        return lo, mid

    lo0, mid0 = pcts(past[-1])
    lo1, mid1 = pcts(w.latest)
    if None in (lo0, mid0, lo1, mid1) or not lo0 or not mid0:
        return None
    return (lo1 - lo0) / lo0 - (mid1 - mid0) / mid0


# --------------------------------------------------------------------- rules

def buy_everything(w):
    """CONTROL. Buy every event, always. This is the market's drift -- the number
    every real rule must beat."""
    return True


def buy_nothing(w):
    """CONTROL. Never buy. Confirms an empty result is handled cleanly."""
    return False


def momentum(w, min_trend=0.02, min_sales=4):
    """Price is already rising and tickets are moving.

    The simplest possible directional bet: something is working, join it. Risk is
    that you buy the top -- momentum in a decaying asset can reverse hard when the
    late-stage dump starts."""
    t = w.price_trend(7)
    return t is not None and t >= min_trend and w.sales_per_day(7) >= min_sales


def squeeze(w, max_listing_trend=-0.10, min_sales=3):
    """Supply is shrinking while tickets keep selling.

    The core thesis: the book is draining faster than it refills, so whoever still
    needs seats has to pay up. Requires BOTH conditions -- a shrinking book with no
    sales is sellers giving up and delisting, which is bearish, not bullish."""
    lt = _listing_trend(w, 7)
    return lt is not None and lt <= max_listing_trend and w.sales_per_day(7) >= min_sales


def primary_sold_out(w, min_sales=2):
    """Ticketmaster's primary inventory is gone.

    While face-value seats exist they cap the resale price. When primary empties,
    that ceiling lifts. Skips events where the source can't tell primary from
    secondary rather than guessing."""
    pc = _primary_count(w)
    return pc is not None and pc == 0 and w.sales_per_day(7) >= min_sales


def cheap_tier_eroding(w, min_erosion=0.03, min_sales=2):
    """The bottom of the ladder is lifting faster than the middle.

    Buyers eat the cheapest seats first, so this tends to lead the headline price.
    Earlier signal than momentum, and correspondingly noisier."""
    e = _cheap_tier_trend(w, 7)
    return e is not None and e >= min_erosion and w.sales_per_day(7) >= min_sales


def dead_zone_value(w, min_days=21, max_days=45, min_sales=5,
                    max_trend=-0.02):
    """Mid-lifecycle trough on an event that is still selling well.

    Prices sag in the middle stretch after brokers have listed and before urgency
    kicks in. An event selling briskly through its own trough is a candidate to
    spike late. This is the 'buy the dip' version -- it deliberately buys weakness,
    so it is the opposite bet from momentum and should be compared against it."""
    if not (min_days <= w.days_to_event <= max_days):
        return False
    t = w.price_trend(7)
    return t is not None and t <= max_trend and w.sales_per_day(7) >= min_sales


def thin_float(w, max_listings=60, min_sales=3):
    """Very little inventory left relative to remaining time.

    Crude version of days-of-supply: few listings plus steady sales means it runs
    out. Absolute threshold makes this venue-size dependent -- it will need
    capacity normalization once seatmap data is available."""
    return w.listing_count() <= max_listings and w.sales_per_day(7) >= min_sales


def accelerating_demand(w, min_ratio=1.5, min_sales=3):
    """Sales are speeding up: the last week outpaced the week before.

    Second derivative rather than level. Catches events waking up, which is what
    you want before the price reflects it."""
    recent = w.sales_per_day(7)
    prior_total = w.sales_per_day(14) * 14 - recent * 7
    prior = prior_total / 7.0
    if prior <= 0:
        return recent >= min_sales
    return recent >= min_sales and (recent / prior) >= min_ratio


RULES = {
    "buy_everything": buy_everything,
    "buy_nothing": buy_nothing,
    "momentum": momentum,
    "squeeze": squeeze,
    "primary_sold_out": primary_sold_out,
    "cheap_tier_eroding": cheap_tier_eroding,
    "dead_zone_value": dead_zone_value,
    "thin_float": thin_float,
    "accelerating_demand": accelerating_demand,
}


def compare(events, rules=None, **kw):
    """Run every rule over the same events and rank by edge over buy-everything.

    Ranking on EDGE rather than raw return is the whole discipline: in a rising
    market every rule looks smart, and in a falling one every rule looks broken.
    Only the spread against the control means anything."""
    rules = rules or RULES
    rows = []
    for name, fn in rules.items():
        r = run(events, fn, **kw)
        rows.append((name, r))
    rows.sort(key=lambda kv: (kv[1]["edge_vs_base"] is not None,
                              kv[1]["edge_vs_base"] or 0), reverse=True)

    out = [f"{'rule':<22}{'signals':>8}{'hit':>7}{'return':>9}"
           f"{'unsold':>8}{'edge':>9}", "-" * 63]
    for name, r in rows:
        def f(x, pct=True):
            if x is None:
                return "   n/a"
            return f"{x:+.1%}" if pct else f"{x:.0%}"
        out.append(
            f"{name:<22}{r['signals']:>8}"
            f"{f(r['hit_rate'], False):>7}"
            f"{f(r['mean_return']):>9}"
            f"{f(r['unsold_rate'], False):>8}"
            f"{f(r['edge_vs_base']):>9}"
        )
    out.append("-" * 63)
    out.append("edge = mean return minus buy_everything. Only this column matters.")
    return "\n".join(out)


# ------------------------------------------------------------------ self-test

def _selftest():
    from backtest import _mk
    print("rules.py self-test\n" + "-" * 63)
    day0 = 1_700_000_000.0
    events = ([_mk(f"up{i}", day0, True) for i in range(8)]
              + [_mk(f"dn{i}", day0, False) for i in range(8)])

    print(compare(events, decide_days_before=20, horizon_days=10))

    # Every rule must at least execute without error and return a bool.
    w = events[0].window_at(events[0].event_time - 20 * 86400.0)
    for name, fn in RULES.items():
        v = fn(w)
        assert isinstance(v, bool), f"{name} returned {type(v)}"

    r_all = run(events, buy_everything, decide_days_before=20, horizon_days=10)
    r_none = run(events, buy_nothing, decide_days_before=20, horizon_days=10)
    assert r_all["signals"] == len(events), r_all["signals"]
    assert r_none["signals"] == 0 and r_none["hit_rate"] is None
    assert abs(r_all["edge_vs_base"]) < 1e-9, "control must have zero edge itself"

    r_mom = run(events, momentum, decide_days_before=20, horizon_days=10)
    assert r_mom["signals"] > 0 and r_mom["edge_vs_base"] > 0
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
