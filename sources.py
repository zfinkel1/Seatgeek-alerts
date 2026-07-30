"""
Niche-primary event sources: the supply side of the arbitrage.

THE PATTERN
An operator sells at face because ticketing isn't their business -- a nightclub,
a lecture series, a supper club. The same event trades higher on SeatGeek because
that's where resale demand lives. The gap is visible at purchase; no forecasting.

Eventbrite is deliberately NOT here. Every broker watches it, so the edge is gone.
These sources are picked for the opposite reason: nobody is looking.

TAO GROUP -- the first source, chosen because it's a trade that already worked by
hand. tickets.taogroup.com is server-rendered with ~7,000 events across Las Vegas,
New York and Chicago (TAO, Marquee, OMNIA, Hakkasan, JEWEL, Palm Tree). No bot
protection, no JS required. Its Cloudinary assets sit under `eventservice/saas/
partner-logos/`, i.e. it is a WHITE-LABEL ticketing SaaS -- so the same parser
should light up other hospitality groups on the same platform. That's the reason
to shape this as a pluggable source rather than a one-off script.

WHAT THIS DOES NOT DO
Fetch face prices. Those live on individual event pages, one request each, so
they're pulled only for events that already look interesting -- there's no sense
hitting 7,000 pages to price events whose performer has no resale value.
"""
import re
import html as _html
import urllib.request
from datetime import datetime

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"

# Guest-list entries are free RSVPs, not sellable inventory. They roughly double
# the raw event count and would otherwise pollute every downstream score.
_SKIP = re.compile(r"guest\s*list", re.I)

# Recurring club-night branding rather than a bookable name. "Daycation
# Thursdays" has no resale value; "Mike Posner" does. Not a hard filter -- it
# only lowers confidence, since a residency can still carry a real headliner.
_GENERIC = re.compile(
    r"\b(thursdays?|fridays?|saturdays?|sundays?|mondays?|tuesdays?|wednesdays?|"
    r"daycation|brunch|pool party|day ?club|night ?club|terrace|guest ?list)\b", re.I)


def _get(url, timeout=60):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _clean(fragment):
    return _html.unescape(
        re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", fragment))).strip()


def _date_from_slug(slug):
    """Slugs carry the date: 'tao-nightclub-las-vegas-7-30-2026'. Some don't
    ('mike-posner-tao') -- those return None and get dated from the page later."""
    m = re.search(r"(\d{1,2})-(\d{1,2})-(20\d\d)", slug)
    if not m:
        return None
    try:
        return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).date()
    except ValueError:
        return None


def _venue_from_slug(slug):
    s = slug.lstrip("/").replace("e/", "", 1)
    s = re.sub(r"-\d{1,2}-\d{1,2}-20\d\d$", "", s)
    return s.replace("-", " ").strip()


def tao_group(url="https://tickets.taogroup.com/"):
    """Every listed Tao Group event: {source, title, url, slug, date, venue_hint}.

    Dedupes on slug -- the index renders each event in several layouts (grid,
    list, carousel), so the raw anchor count is many times the real event count.
    """
    raw = _get(url)
    out, seen = [], set()
    for href, inner in re.findall(
            r'<a[^>]+href="([^"]*/e/[^"]*)"[^>]*>(.*?)</a>', raw, re.S | re.I):
        slug = href.split("?")[0].rstrip("/")
        title = _clean(inner)
        if not title or len(title) < 4 or slug in seen:
            continue
        if _SKIP.search(title) or _SKIP.search(slug):
            continue
        seen.add(slug)
        out.append({
            "source": "taogroup",
            "title": title,
            "slug": slug,
            "url": "https://tickets.taogroup.com" + slug
                   if slug.startswith("/") else slug,
            "date": _date_from_slug(slug),
            "venue_hint": _venue_from_slug(slug),
            "generic": bool(_GENERIC.search(title)),
        })
    return out


def performer_candidates(events):
    """Events whose title looks like a bookable act rather than a club night.

    Splits on 'ft.'/'presents'/'with' and keeps the lead name, so
    'Mike Posner ft. Brody Jenner' resolves to 'Mike Posner' -- which is what a
    SeatGeek performer lookup needs."""
    out = []
    for e in events:
        if e["generic"]:
            continue
        name = re.split(r"\s+(?:ft\.?|feat\.?|featuring|presents|with|w/|:|\|)\s+",
                        e["title"], maxsplit=1)[0].strip()
        name = re.sub(r"\s*\(.*?\)\s*", " ", name).strip(" -–—")
        if len(name) < 3 or _GENERIC.search(name):
            continue
        out.append({**e, "performer": name})
    return out


def event_detail(url):
    """Price ladder + remaining inventory for one event, from schema.org JSON-LD.

    These operators sell in TIERS -- GA Tier 1 $25, Tier 2 $30, ... Tier 6 $50 --
    and tiers sell out in order. That gives two things a raw ticket count would
    not:

      * `price` is the cheapest tier still InStock, i.e. what you'd actually pay
        right now, not a headline number.
      * `sold_out_tiers` IS the depletion curve. Re-read this page daily and the
        rate at which tiers fall is the primary-side sell-through signal -- the
        same 'is it running out' question we ask on secondary, answered directly.

    An event where tier 5 of 6 is live three weeks out is behaving very
    differently from one still sitting on tier 1.
    """
    raw = _get(url)
    offers = []
    # The ld+json block is duplicated across page layouts; the offers arrays are
    # identical, so parse the first and dedupe by (name, price).
    for m in re.finditer(r'"offers"\s*:\s*(\[.*?\])\s*[,}]', raw, re.S):
        try:
            import json as _json
            parsed = _json.loads(m.group(1).replace("\\/", "/"))
        except Exception:
            continue
        for o in parsed:
            if not isinstance(o, dict) or o.get("price") is None:
                continue
            offers.append({
                "name": _html.unescape(str(o.get("name") or "")).strip(),
                "price": float(o["price"]),
                "in_stock": "InStock" in str(o.get("availability") or ""),
                "category": o.get("category"),
            })
        if offers:
            break

    uniq, seen = [], set()
    for o in offers:
        k = (o["name"], o["price"])
        if k not in seen:
            seen.add(k)
            uniq.append(o)
    uniq.sort(key=lambda o: o["price"])

    live = [o for o in uniq if o["in_stock"]]
    gone = [o for o in uniq if not o["in_stock"]]
    return {
        "url": url,
        "tiers": uniq,
        "price": live[0]["price"] if live else None,       # cheapest buyable
        "top_price": uniq[-1]["price"] if uniq else None,
        "tiers_total": len(uniq),
        "sold_out_tiers": len(gone),
        # How far up the ladder the event has climbed. 0 = nothing moving,
        # 1 = everything gone. The cleanest primary demand read available.
        "depletion": (len(gone) / len(uniq)) if uniq else None,
        "sold_out": bool(uniq) and not live,
    }


SOURCES = {"taogroup": tao_group}


def _demo():
    evs = tao_group()
    cands = performer_candidates(evs)
    print(f"Tao Group: {len(evs)} sellable events "
          f"(guest lists removed), {len(cands)} with a bookable performer\n")
    print(f"{'date':<12}{'performer':<34}{'venue hint':<34}")
    print("-" * 80)
    seen = set()
    for c in cands:
        if c["performer"].lower() in seen:
            continue
        seen.add(c["performer"].lower())
        d = c["date"].isoformat() if c["date"] else "?"
        print(f"{d:<12}{c['performer'][:33]:<34}{c['venue_hint'][:33]:<34}")
        if len(seen) >= 25:
            break
    print(f"\n{len(seen)} distinct performer names shown; "
          f"{len({c['performer'].lower() for c in cands})} total")


def _selftest():
    assert _date_from_slug("/e/tao-nightclub-las-vegas-7-30-2026").isoformat() \
        == "2026-07-30"
    assert _date_from_slug("/e/mike-posner-tao") is None
    assert "las vegas" in _venue_from_slug("/e/tao-nightclub-las-vegas-7-30-2026")
    fake = [
        {"title": "Mike Posner ft. Brody Jenner", "generic": False, "slug": "a",
         "date": None, "url": "", "source": "t", "venue_hint": ""},
        {"title": "Daycation Thursdays", "generic": True, "slug": "b",
         "date": None, "url": "", "source": "t", "venue_hint": ""},
    ]
    c = performer_candidates(fake)
    assert len(c) == 1 and c[0]["performer"] == "Mike Posner", c
    print("sources.py self-test: ALL PASS\n")


if __name__ == "__main__":
    _selftest()
    _demo()
