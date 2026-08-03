"""
Does this performer's track record travel?

THE BUG THIS FIXES
Presale scoring keyed history on WHO is playing, when what actually predicted the
profit was WHERE. Two cases from one alert email:

  Disney On Ice   27 past shows, +44% margin, 78% profitable
                  -> transfers fine. Same production, same seat map, same
                     audience in Tucson, Boston and Worcester.

  Blackhawks      profitable record, all of it Chicago home games
                  -> does NOT transfer. That profit came from being a Chicago
                     broker with Chicago buyers. A Blackhawks road game in
                     Nashville inherits none of it, but the scorer surfaced every
                     Blackhawks game in the country.

THE TEST
Not a hand-maintained list of teams -- count distinct metros. A performer whose
events spread across many cities tours; one that plays the same city over and
over is resident there. The data already says which, so nothing needs curating.

Resident performers keep their history ONLY in their home market. Everywhere
else they score as an unknown, on comps alone.
"""
import re
import statistics
from collections import Counter

# Share of a performer's events in ONE metro that marks it as home.
#
# Set from how sports actually schedule: an NHL/NBA team plays roughly half its
# games at home, so ~0.5 is the real signal. Counting distinct metros does NOT
# work -- the Blackhawks play 36 of them on the road, which made the first
# version classify them as a touring act, i.e. the exact bug this file exists to
# fix. Concentration decides; breadth is noise.
HOME_SHARE = 0.40

_STATES = {
    "alabama": "al", "alaska": "ak", "arizona": "az", "arkansas": "ar",
    "california": "ca", "colorado": "co", "connecticut": "ct", "delaware": "de",
    "florida": "fl", "georgia": "ga", "hawaii": "hi", "idaho": "id",
    "illinois": "il", "indiana": "in", "iowa": "ia", "kansas": "ks",
    "kentucky": "ky", "louisiana": "la", "maine": "me", "maryland": "md",
    "massachusetts": "ma", "michigan": "mi", "minnesota": "mn",
    "mississippi": "ms", "missouri": "mo", "montana": "mt", "nebraska": "ne",
    "nevada": "nv", "new hampshire": "nh", "new jersey": "nj",
    "new mexico": "nm", "new york": "ny", "north carolina": "nc",
    "north dakota": "nd", "ohio": "oh", "oklahoma": "ok", "oregon": "or",
    "pennsylvania": "pa", "rhode island": "ri", "south carolina": "sc",
    "south dakota": "sd", "tennessee": "tn", "texas": "tx", "utah": "ut",
    "vermont": "vt", "virginia": "va", "washington": "wa",
    "west virginia": "wv", "wisconsin": "wi", "wyoming": "wy",
    "district of columbia": "dc",
}


def _metro(event):
    """City+state as the market key, or None when the geo is unusable.

    Real feeds are dirty in two ways that both split one market into several:
    the state arrives as a full name on some rows and an abbreviation on others
    ('chicago, illinois' vs 'chicago, il'), and sometimes the venue name lands
    in the city field with a ZIP in the state ('united center, 60612'). Left
    alone, Chicago's 122 Blackhawks dates fragmented into three buckets and no
    single metro looked dominant.

    Rows with a ZIP where a state belongs are dropped rather than guessed at --
    a wrong market is worse than a missing one, since it dilutes the real home.
    """
    city = (event.get("venue_city") or event.get("city") or "").strip().lower()
    state = (event.get("venue_state") or event.get("state") or "").strip().lower()
    if not city:
        return None
    if re.fullmatch(r"\d{5}(-\d{4})?", state):
        return None                       # ZIP in the state field -> unusable
    state = _STATES.get(state, state)
    city = re.sub(r"\s*\d{5}(-\d{4})?\s*$", "", city).strip(" ,")
    if not city:
        return None
    return f"{city}, {state}" if state else city


def classify(events):
    """touring | resident | unknown, plus the home market when resident.

    `events` is any list of dicts carrying venue_city (and ideally venue_state) --
    SeatData search results work as-is.
    """
    metros = [m for m in (_metro(e) for e in events) if m]
    if len(metros) < 3:
        # Too little to tell. Treated as non-transferring, which is the safe
        # direction: an unknown scores on comps rather than borrowed history.
        return {"type": "unknown", "home": None, "metros": len(set(metros)),
                "events": len(events)}
    counts = Counter(metros)
    top_metro, top_n = counts.most_common(1)[0]
    share = top_n / len(metros)
    distinct = len(counts)
    runner_up = counts.most_common(2)[1][1] if distinct > 1 else 0

    # Two ways to be resident, because concentration shows up differently:
    #   - share: half your dates in one metro (a team's home schedule)
    #   - dominance: your top metro dwarfs every other (Chicago 122 vs the next
    #     city's 5). A tour is roughly flat across its cities; a team is not.
    dominant = runner_up and (top_n / runner_up) >= 4
    if share >= HOME_SHARE or dominant:
        kind, home = "resident", top_metro
    else:
        kind, home = "touring", None
    return {"type": kind, "home": home, "metros": distinct,
            "events": len(events), "home_share": round(share, 3),
            "dominance": round(top_n / runner_up, 1) if runner_up else None,
            "top_metros": counts.most_common(4)}


def history_transfers(profile, target_city, target_state=None):
    """Should this performer's past performance count toward THIS event's score?

    Touring acts: yes, anywhere. Resident acts: only at home. Unknown: no --
    scoring an unknown off history we can't validate is how the Blackhawks noise
    got in.
    """
    if profile["type"] == "touring":
        return True
    if profile["type"] != "resident" or not profile["home"]:
        return False
    target = _metro({"venue_city": target_city, "venue_state": target_state})
    return bool(target) and target == profile["home"]


def popularity(events, sales_by_event=None):
    """How much demand this performer actually moves.

    Ranks on TICKETS SOLD before anything else. Number of shows measures how much
    they work; tickets measure how much the market wants them, and only the
    second one sizes a position. Falls back to show count when no sales are
    available, and says so via `basis` rather than pretending."""
    n_events = len(events)
    if not sales_by_event:
        return {"score": float(n_events), "basis": "events only",
                "events": n_events, "tickets": None, "median_price": None}
    tickets, prices, with_sales = 0, [], 0
    for rows in sales_by_event.values():
        if not rows:
            continue
        with_sales += 1
        tickets += sum(r.get("quantity") or 0 for r in rows)
        prices += [r["price"] for r in rows if r.get("price")]
    per_event = (tickets / with_sales) if with_sales else 0.0
    return {
        # Tickets-per-event times a damped event count: a performer who moves 80
        # a night across 30 shows outranks one who moves 80 across two.
        "score": round(per_event * (n_events ** 0.5), 1),
        "basis": "tickets sold",
        "events": n_events,
        "events_with_sales": with_sales,
        "tickets": tickets,
        "per_event": round(per_event, 1),
        "median_price": round(statistics.median(prices), 2) if prices else None,
    }


def score_adjustment(profile, target_city, target_state=None):
    """What the presale scorer should do with this row.

    Returns (multiplier, reason). The multiplier scales whatever history-derived
    component the existing score already has -- it doesn't replace the score, so
    this can be dropped into an existing pipeline without rewriting it."""
    if history_transfers(profile, target_city, target_state):
        if profile["type"] == "touring":
            return 1.0, f"touring act ({profile['metros']} metros) — history applies"
        return 1.0, f"home market ({profile['home']}) — history applies"
    if profile["type"] == "resident":
        return 0.0, (f"resident to {profile['home']} — no record in "
                     f"{target_city}; scoring on comps only")
    return 0.0, "too few markets to judge — scoring on comps only"


def _selftest():
    print("markets.py self-test\n" + "-" * 62)

    touring = [{"venue_city": c, "venue_state": s} for c, s in [
        ("Tucson", "AZ"), ("Boston", "MA"), ("Worcester", "MA"),
        ("Denver", "CO"), ("Seattle", "WA"), ("Dallas", "TX"),
        ("Miami", "FL"), ("Chicago", "IL")]]
    p = classify(touring)
    print(f"Disney-like : {p['type']:<9} metros={p['metros']} home={p['home']}")
    assert p["type"] == "touring"
    assert history_transfers(p, "Tucson", "AZ")
    assert history_transfers(p, "Nashville", "TN")   # travels anywhere

    resident = ([{"venue_city": "Chicago", "venue_state": "IL"}] * 40
                + [{"venue_city": "Nashville", "venue_state": "TN"}]
                + [{"venue_city": "Detroit", "venue_state": "MI"}])
    p2 = classify(resident)
    print(f"Blackhawks  : {p2['type']:<9} metros={p2['metros']} "
          f"home={p2['home']} share={p2['home_share']}")
    assert p2["type"] == "resident" and p2["home"] == "chicago, il"
    assert history_transfers(p2, "Chicago", "IL"), "home must still count"
    assert not history_transfers(p2, "Nashville", "TN"), "THE BUG — must not travel"

    mult, why = score_adjustment(p2, "Nashville", "TN")
    print(f"  -> Nashville: x{mult}  {why}")
    assert mult == 0.0
    mult2, why2 = score_adjustment(p2, "Chicago", "IL")
    print(f"  -> Chicago:   x{mult2}  {why2}")
    assert mult2 == 1.0

    thin = [{"venue_city": "Chicago", "venue_state": "IL"}]
    p3 = classify(thin)
    assert p3["type"] == "unknown" and not history_transfers(p3, "Chicago", "IL")
    print(f"one event   : {p3['type']} — no borrowed history")

    # popularity ranks on tickets moved, not number of shows
    busy = {1: [{"quantity": 2, "price": 100}] * 3}          # 6 tickets, 1 event
    big = {i: [{"quantity": 40, "price": 100}] for i in range(4)}  # 160 over 4
    a = popularity([{}], busy)
    b = popularity([{}] * 4, big)
    print(f"\npopularity  : busy={a['score']}  big={b['score']}  "
          f"(basis {b['basis']})")
    assert b["score"] > a["score"]
    assert popularity([{}] * 9)["basis"] == "events only"
    print("\nALL PASS")


if __name__ == "__main__":
    _selftest()
