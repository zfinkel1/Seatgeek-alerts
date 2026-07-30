"""
Walk-forward backtest harness.

THE POINT
Before real money goes in, one question has to be answered honestly: if this rule
had been running six months ago, would it have made money net of fees? Everything
else -- the scanner, the alerts, the zone maps -- is worthless if the answer is no,
and expensive if the answer is unknown.

THE GUARANTEE
The single way a backtest lies to you is look-ahead: the strategy accidentally sees
data from after the decision point and "predicts" something it was told. Every
backtest that looks too good has this bug.

So look-ahead is prevented STRUCTURALLY here, not by convention. A strategy is
handed a `Window` containing only observations at or before its decision time. The
future is not passed to it and filtered -- it is never in the object at all. There
is no attribute on `Window` that reaches the outcome data. `_selftest` includes a
strategy that tries to cheat and verifies it cannot.

SOURCE-AGNOSTIC
Takes plain dicts, so it runs on TicketGenie API pulls, a CSV export, or the local
history.py files. Vendors keep changing; the harness shouldn't have to.

    snapshots: [{"t": epoch, "listings": [{"section","price","qty",...}]}, ...]
    sales:     [{"t": epoch, "atp": float, "quantity": int, "section": str}, ...]
"""
import math
import statistics
from bisect import bisect_right

FEE_PCT = 0.08          # founder's actual all-in broker take rate
DAY = 86400.0


class Window:
    """Everything knowable at decision time, and nothing else.

    Deliberately holds no reference to the parent Event, so there is no attribute
    path from a strategy back to future data."""

    __slots__ = ("event_id", "t", "days_to_event", "snapshots", "sales", "meta")

    def __init__(self, event_id, t, days_to_event, snapshots, sales, meta):
        self.event_id = event_id
        self.t = t
        self.days_to_event = days_to_event
        self.snapshots = snapshots      # ascending by t, all t <= decision time
        self.sales = sales              # ascending by t, all t <= decision time
        self.meta = meta

    @property
    def latest(self):
        return self.snapshots[-1] if self.snapshots else None

    def get_in(self):
        """Cheapest available ask right now -- what you'd actually pay."""
        snap = self.latest
        if not snap:
            return None
        prices = [float(L["price"]) for L in snap.get("listings", ())
                  if L.get("price") is not None]
        return min(prices) if prices else None

    def listing_count(self):
        snap = self.latest
        return len(snap.get("listings", ())) if snap else 0

    def sales_per_day(self, days=7):
        """Clearing rate over the trailing window -- the demand observation."""
        if not self.sales:
            return 0.0
        cutoff = self.t - days * DAY
        qty = sum(s.get("quantity") or 0 for s in self.sales if s["t"] >= cutoff)
        return qty / float(days)

    def price_trend(self, days=7):
        """Fractional change in get-in over the trailing window."""
        if len(self.snapshots) < 2:
            return None
        cutoff = self.t - days * DAY
        past = [s for s in self.snapshots if s["t"] <= cutoff] or [self.snapshots[0]]

        def cheapest(snap):
            ps = [float(L["price"]) for L in snap.get("listings", ())
                  if L.get("price") is not None]
            return min(ps) if ps else None

        then, now = cheapest(past[-1]), self.get_in()
        if not then or not now:
            return None
        return (now - then) / then


class Event:
    """One event's full timeline. Only the harness sees the whole thing."""

    def __init__(self, event_id, event_time, snapshots, sales, meta=None):
        self.event_id = event_id
        self.event_time = event_time
        self.snapshots = sorted(snapshots, key=lambda s: s["t"])
        self.sales = sorted(sales, key=lambda s: s["t"])
        self.meta = meta or {}
        self._snap_ts = [s["t"] for s in self.snapshots]
        self._sale_ts = [s["t"] for s in self.sales]

    def window_at(self, t):
        """Strictly-past view. bisect_right includes observations exactly at t
        (you can act on what just printed) and nothing after."""
        si = bisect_right(self._snap_ts, t)
        qi = bisect_right(self._sale_ts, t)
        if si == 0:
            return None                      # nothing observed yet -- can't decide
        return Window(
            self.event_id, t, (self.event_time - t) / DAY,
            self.snapshots[:si], self.sales[:qi], dict(self.meta),
        )

    def _cleared(self, lo, hi, fee_pct):
        """Volume-weighted net price of CONFIRMED sales in a window. Uses sales,
        not asks -- you cannot sell into an ask."""
        rows = [s for s in self.sales if lo < s["t"] <= hi and s.get("atp")]
        if not rows:
            return None
        qty = sum(s.get("quantity") or 1 for s in rows)
        gross = sum(float(s["atp"]) * (s.get("quantity") or 1) for s in rows)
        return (gross / qty) * (1 - fee_pct)

    def exit_price(self, t_from, horizon_days, fee_pct=FEE_PCT):
        """What you net, modelling how the desk actually exits.

        A broker does not hold a ticket until it expires -- they mark it down
        until it clears. So an unsold position is not a 100% loss, it is a worse
        price. Two-stage:

          1. TARGET: sell within `horizon_days` at the market price then.
          2. MARKDOWN: if nothing cleared in that window, hold and take whatever
             the market paid closest to the event. That is the discounted exit.

        Only if NOTHING ever clears before the event is it a genuine write-off,
        which does happen on events with no bid at any price.

        Returns (net_price, how) where how is 'target' | 'markdown' | 'writeoff'.
        """
        lo = t_from
        target_hi = min(t_from + horizon_days * DAY, self.event_time)
        net = self._cleared(lo, target_hi, fee_pct)
        if net is not None:
            return net, "target"
        # Fall back to the last stretch before the event -- the dump window.
        net = self._cleared(target_hi, self.event_time, fee_pct)
        if net is not None:
            return net, "markdown"
        return None, "writeoff"


def run(events, strategy, *, decide_days_before=30, horizon_days=14,
        fee_pct=FEE_PCT):
    """Walk each event forward to its decision point, ask the strategy, score it.

    strategy(window) -> True to buy, or a dict {"buy": bool, ...} for extra detail.
    """
    trades, skipped = [], 0
    for ev in events:
        t = ev.event_time - decide_days_before * DAY
        w = ev.window_at(t)
        if w is None:
            skipped += 1
            continue
        entry = w.get_in()
        if entry is None or entry <= 0:
            skipped += 1
            continue

        verdict = strategy(w)
        buy = verdict.get("buy") if isinstance(verdict, dict) else bool(verdict)
        if not buy:
            continue

        exit_net, how = ev.exit_price(t, horizon_days, fee_pct)
        trades.append({
            "event_id": ev.event_id, "entry": entry, "exit": exit_net,
            "how": how,
            # A write-off is the only -100%. A markdown is a real (often bad)
            # price, not a wipeout -- scoring it as -100% would badly overstate
            # risk for a desk that simply cuts price until it clears.
            "ret": -1.0 if exit_net is None else (exit_net - entry) / entry,
        })
    return _report(trades, skipped, events, decide_days_before,
                   horizon_days, fee_pct)


def _base_rate(events, decide_days_before, horizon_days, fee_pct):
    """What buying EVERY event blindly would have returned. A strategy that can't
    beat this has no edge -- it just re-derives the market's average drift."""
    rets = []
    for ev in events:
        t = ev.event_time - decide_days_before * DAY
        w = ev.window_at(t)
        if w is None:
            continue
        entry = w.get_in()
        if not entry:
            continue
        ex, _ = ev.exit_price(t, horizon_days, fee_pct)
        rets.append(-1.0 if ex is None else (ex - entry) / entry)
    return (statistics.mean(rets) if rets else None), len(rets)


def _report(trades, skipped, events, ddb, hz, fee):
    closed = [t for t in trades if t["ret"] is not None]
    rets = [t["ret"] for t in closed]
    on_target = sum(1 for t in trades if t["how"] == "target")
    marked_down = sum(1 for t in trades if t["how"] == "markdown")
    no_exit = sum(1 for t in trades if t["how"] == "writeoff")
    base, base_n = _base_rate(events, ddb, hz, fee)
    wins = [r for r in rets if r > 0]
    return {
        "events": len(events),
        "skipped_no_data": skipped,
        "signals": len(trades),
        "closed": len(closed),
        # Sold at the target horizon vs had to be marked down vs never cleared.
        # Markdown rate is the number the desk actually feels: capital tied up
        # and a worse price, but not a wipeout.
        "on_target": on_target,
        "marked_down": marked_down,
        "markdown_rate": (marked_down / len(trades)) if trades else None,
        "unsold": no_exit,
        "unsold_rate": (no_exit / len(trades)) if trades else None,
        "hit_rate": (len(wins) / len(rets)) if rets else None,
        "mean_return": statistics.mean(rets) if rets else None,
        "median_return": statistics.median(rets) if rets else None,
        "worst": min(rets) if rets else None,
        "best": max(rets) if rets else None,
        "base_rate_return": base,
        "base_rate_n": base_n,
        # The number that decides it. Beating the base rate is the only evidence
        # of edge; everything else can be luck or a rising market.
        "edge_vs_base": (statistics.mean(rets) - base)
                        if (rets and base is not None) else None,
        "fee_pct": fee,
        "decide_days_before": ddb,
        "horizon_days": hz,
    }


def format_report(r):
    def pct(x):
        return "n/a" if x is None else f"{x:+.1%}"

    def rate(x):
        return "n/a" if x is None else f"{x:.0%}"

    return "\n".join([
        f"events={r['events']}  signals={r['signals']}  closed={r['closed']}"
        f"  skipped={r['skipped_no_data']}",
        f"decision at T-{r['decide_days_before']}d, exit at +{r['horizon_days']}d,"
        f" fee {r['fee_pct']:.0%}",
        f"  hit rate      {rate(r['hit_rate'])}",
        f"  mean return   {pct(r['mean_return'])}",
        f"  median        {pct(r['median_return'])}",
        f"  best / worst  {pct(r['best'])} / {pct(r['worst'])}",
        f"  sold on time  {r['on_target']}",
        f"  marked down   {r['marked_down']} ({rate(r['markdown_rate'])})"
        f"   <- cleared, but at a worse price",
        f"  WRITE-OFF     {r['unsold']} ({rate(r['unsold_rate'])})"
        f"   <- never cleared at any price",
        f"  base rate     {pct(r['base_rate_return'])} (n={r['base_rate_n']})",
        f"  EDGE vs base  {pct(r['edge_vs_base'])}",
    ])


# ------------------------------------------------------------------ self-test

def _mk(event_id, day0, rising, n_days=60, event_day=60):
    """Synthetic event. `rising` flips the late-stage direction so the harness can
    be checked against a known answer."""
    snaps, sales = [], []
    for d in range(n_days):
        t = day0 + d * DAY
        if rising:
            # Steep enough to clear a 15% round trip. A gentler rise still beats
            # the base rate but loses money after fees -- which is the whole
            # reason fees belong inside the harness rather than bolted on after.
            price = 100 - d * 0.5 if d < 30 else 85 + (d - 30) * 6.0
        else:
            price = 100 - d * 1.0
        snaps.append({"t": t, "listings": [
            {"section": "100", "price": round(price, 2), "qty": 2},
            {"section": "101", "price": round(price * 1.2, 2), "qty": 2},
        ]})
        sales.append({"t": t, "atp": round(price * 1.02, 2),
                      "quantity": 6 if rising else 2, "section": "100"})
    return Event(event_id, day0 + event_day * DAY, snaps, sales)


def _selftest():
    print("backtest.py self-test\n" + "-" * 64)
    day0 = 1_700_000_000.0
    events = ([_mk(f"up{i}", day0, True) for i in range(6)]
              + [_mk(f"dn{i}", day0, False) for i in range(6)])

    # 1. Look-ahead must be structurally impossible.
    leaked = {"seen": False}

    def cheater(w):
        # Try every plausible route to the future. None must exist.
        for attr in ("event", "_event", "parent", "future", "outcome", "all_sales"):
            if hasattr(w, attr):
                leaked["seen"] = True
        if w.snapshots and w.snapshots[-1]["t"] > w.t:
            leaked["seen"] = True
        if w.sales and w.sales[-1]["t"] > w.t:
            leaked["seen"] = True
        return False

    run(events, cheater)
    print(f"look-ahead reachable from Window: {leaked['seen']}")
    assert not leaked["seen"], "STRATEGY COULD SEE THE FUTURE -- harness is invalid"

    # 2. A momentum rule should find the rising cohort and beat blind buying.
    def momentum(w):
        trend = w.price_trend(7)
        return {"buy": trend is not None and trend > 0.01
                and w.sales_per_day(7) >= 4}

    r = run(events, momentum, decide_days_before=20, horizon_days=10)
    print(format_report(r))
    assert r["signals"] > 0, "strategy never fired"
    assert r["mean_return"] > 0, r["mean_return"]
    assert r["edge_vs_base"] > 0, "no edge over buying everything"

    # 3. Unsold inventory must be counted as its own outcome, never as 0% return.
    dead = Event("dead", day0 + 40 * DAY,
                 [{"t": day0 + d * DAY,
                   "listings": [{"section": "1", "price": 50.0, "qty": 2}]}
                  for d in range(30)],
                 [])                      # no sales ever -> no exit
    r2 = run([dead], lambda w: True, decide_days_before=20, horizon_days=10)
    print(f"\nno-liquidity event -> signals={r2['signals']} "
          f"closed={r2['closed']} unsold={r2['unsold']}")
    assert r2["unsold"] == 1 and r2["marked_down"] == 0, r2

    # 4. Fees must actually bite.
    r3 = run(events, lambda w: True, decide_days_before=20, horizon_days=10,
             fee_pct=0.0)
    r4 = run(events, lambda w: True, decide_days_before=20, horizon_days=10,
             fee_pct=0.15)
    print(f"mean return  no-fee={r3['mean_return']:+.1%}  "
          f"15%-fee={r4['mean_return']:+.1%}")
    assert r4["mean_return"] < r3["mean_return"], "fees not applied"

    print("-" * 64)
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
