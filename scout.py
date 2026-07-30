"""
Scout: find events on sites nobody watches, alert on the notable ones.

    crawl sites -> new event? -> is the performer notable? -> text me

That's the whole thing. No valuation model, no forecasting. Notability is
measured, not guessed: Wikipedia pageviews are a real number that exists before
any resale market does.

WHY WIKIPEDIA AND NOT RESALE LISTINGS
Waiting for an event to show resale listings means you are never first -- the
listings ARE other brokers. Andy Cohen ran 2,353 pageviews/day before any market
existed; the 249 listings showed up afterwards. Pageviews are available on day
one, which is when you need to decide.

It also filters noise for free: a club DJ who outranked Andy Cohen on SeatGeek's
popularity score has no Wikipedia page at all.

    python scout.py            # scan, alert on new notable events
    python scout.py --dry      # print, don't send
"""
import os
import re
import sys
import json
import time
import html as H
import urllib.parse
import urllib.request
from datetime import date, timedelta

UA = {"User-Agent": "ticket-scout/1.0 (contact: zfinkel1@gmail.com)"}
STATE_FILE = os.path.join(
    os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "scout_seen.json")

# Daily English-Wikipedia pageviews to count as notable. Andy Cohen ~2,350 and
# Chuck Lorre ~2,000 both clear this; the club-DJ noise has no page at all.
MIN_VIEWS = int(os.environ.get("SCOUT_MIN_VIEWS", "600"))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def _get(url, timeout=45):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


# ------------------------------------------------------------------ notability

# A resolved article only counts if it's about someone who performs. Wikipedia's
# search will happily return the Prime Minister of India for the comedian "Modi",
# or the sitcom "It's Always Sunny" for a show called "It's Always Punny" -- both
# real false positives from the first run, both scoring higher than Andy Cohen.
PERFORMER_RE = re.compile(
    r"\b(comedian|comic|stand-?up|actor|actress|musician|singer|rapper|"
    r"songwriter|dj|disc jockey|host|presenter|broadcaster|writer|author|"
    r"podcaster|entertainer|performer|television personality|drag queen|"
    r"magician|band|duo|producer|filmmaker|screenwriter)\b", re.I)


def _summary(title):
    url = ("https://en.wikipedia.org/api/rest_v1/page/summary/"
           + urllib.parse.quote(title.replace(" ", "_"), safe=""))
    try:
        return json.loads(_get(url, timeout=20))
    except Exception:
        return None


def resolve_title(name):
    """Wikipedia article for a performer, or None.

    Two guards, both learned from bad matches on the first real run:

    1. NAME MATCH. The resolved title must share the surname with the query.
       Without it 'Modi' resolves to Narendra Modi (10,447 views/day) and
       outranks every genuine act on the list.

    2. OCCUPATION. The article must describe someone who performs. This kills
       TV-show and place-name collisions that survive the name check.

    Title-casing alone is also not enough to find people -- 'mckinnon' becomes
    'Mckinnon' and 404s against 'Kate McKinnon' -- so search still does the
    lookup; it just isn't trusted blindly any more.
    """
    url = ("https://en.wikipedia.org/w/api.php?action=opensearch&limit=3"
           "&namespace=0&format=json&search=" + urllib.parse.quote(name))
    try:
        d = json.loads(_get(url, timeout=20))
        candidates = d[1] if len(d) > 1 else []
    except Exception:
        return None

    q_tokens = [t for t in re.split(r"\W+", name.lower()) if len(t) > 2]
    # A single usable token can't identify anyone. "Corey B" matched the voice
    # actor Corey Burton, "Keysha E." matched something unrelated -- both are
    # truncated stage names, and guessing at them is worse than skipping them.
    if len(q_tokens) < 2:
        return None
    for title in candidates:
        t_tokens = [t for t in re.split(r"\W+", title.lower()) if len(t) > 2]
        # Every part of the queried name must appear, and the article can add at
        # most one token (a middle or last name). Otherwise "Mark Paul" happily
        # becomes "Mark-Paul Gosselaar".
        if not all(t in t_tokens for t in q_tokens):
            continue
        if len(t_tokens) > len(q_tokens) + 1:
            continue
        s = _summary(title)
        if not s or s.get("type") == "disambiguation":
            continue
        blurb = f"{s.get('description', '')} {s.get('extract', '')}"
        if PERFORMER_RE.search(blurb):
            return title
    return None


def pageviews(name, days=90):
    """Average daily English-Wikipedia pageviews. None if there's no article --
    which is itself the answer: no article, not notable enough to resell."""
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=days)
    resolved = resolve_title(name) or name
    title = urllib.parse.quote(resolved.strip().replace(" ", "_"), safe="")
    url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
           f"en.wikipedia/all-access/user/{title}/daily/"
           f"{start:%Y%m%d}/{end:%Y%m%d}")
    try:
        d = json.loads(_get(url, timeout=25))
    except Exception:
        return None
    vals = [i["views"] for i in d.get("items", [])]
    return (sum(vals) // len(vals)) if vals else None


# ---------------------------------------------------------------------- source

def how_to_academy_us():
    """How To Academy's North America events. US only -- we can't sell abroad,
    so their (much larger) UK catalogue is deliberately ignored."""
    raw = _get("https://howtoacademy.com/north-america/")
    out = {}
    for m in re.finditer(
            r'href="https?://howtoacademy\.com/north-america-events/([^"/]+)/?"'
            r'[^>]*>(.*?)</a>', raw, re.S):
        slug = m.group(1)
        title = H.unescape(re.sub(r"\s+", " ",
                                  re.sub(r"<[^>]+>", " ", m.group(2)))).strip()
        out[slug] = {
            "id": f"htaus:{slug}",
            "source": "How To Academy US",
            "title": title or slug.replace("-", " ").title(),
            "url": f"https://howtoacademy.com/north-america-events/{slug}/",
            "performer": performer_from(slug, title),
        }
    return list(out.values())


def the_wilbur():
    """The Wilbur, Boston — comedy and music theatre, ~350 events listed.

    Performer names live in the URL slug ('/event/kevin-nealon/'), which is
    cleaner than the anchor text: the visible link is often an image or a bare
    date."""
    raw = _get("https://thewilbur.com/calendar/")
    out = {}
    for slug in set(re.findall(r'https?://thewilbur\.com/event/([^"/]+)/?"', raw)):
        out[slug] = {
            "id": f"wilbur:{slug}",
            "source": "The Wilbur (Boston)",
            "title": slug.replace("-", " ").title(),
            "url": f"https://thewilbur.com/event/{slug}/",
            "performer": performer_from(slug),
        }
    return list(out.values())


# Helium is a chain, so one parser covers every city it operates in.
# Verified-reachable subdomains only. "stlouis" and "cleveland" fail DNS -- if
# those clubs exist they use different hostnames, and a dead entry costs a DNS
# timeout on every run.
HELIUM_CITIES = ["philadelphia", "portland", "indianapolis", "buffalo"]


def helium(cities=None):
    """Helium Comedy Clubs — national headliners in small rooms, sold direct.

    Here the anchor TEXT carries the name ('Special Event: Colin Quinn') while
    the URL is a numeric id, so this is the mirror image of The Wilbur."""
    out = {}
    for city in (cities or HELIUM_CITIES):
        base = f"https://{city}.heliumcomedy.com"
        try:
            raw = _get(base + "/events")
        except Exception as e:
            print(f"    helium/{city}: {e}")
            continue
        for m in re.finditer(
                r'<a[^>]+href="(?:https?://[^/]+)?/(?:events|shows)/(\d+)"[^>]*>'
                r'(.*?)</a>', raw, re.S):
            eid, inner = m.group(1), m.group(2)
            text = H.unescape(re.sub(r"\s+", " ",
                                     re.sub(r"<[^>]+>", " ", inner))).strip()
            # Many anchors are just "Buy Tickets" — keep the one carrying a name.
            if not text or re.fullmatch(r"(buy tickets|tickets|more info|details)",
                                        text, re.I):
                continue
            name = re.sub(r"^(special event|late show|early show)\s*[:\-]\s*", "",
                          text, flags=re.I).strip()
            key = f"helium:{city}:{eid}"
            if key in out:
                continue
            out[key] = {
                "id": key,
                "source": f"Helium Comedy ({city.title()})",
                "title": text,
                "url": f"{base}/events/{eid}",
                "performer": re.split(r"\s+[-–—|]\s+", name)[0][:40].strip(),
            }
    return list(out.values())


SOURCES = {
    "how_to_academy_us": how_to_academy_us,
    "the_wilbur": the_wilbur,
    "helium": helium,
}


def performer_from(slug, title=""):
    """The billed name, from the slug. Titles are marketing copy ('Twenty Years
    of the Real Housewives'); slugs lead with the person."""
    t = (slug or "").replace("-", " ")
    t = re.split(r"\b(live|in conversation|about|the art of|twenty|on|presents|"
                 r"and no|how i|why|what)\b", t, maxsplit=1)[0]
    t = " ".join(w.capitalize() for w in t.split()).strip()
    return t if 3 < len(t) < 40 else (title or slug)[:40]


# ----------------------------------------------------------------------- state

def load_seen():
    try:
        return set(json.load(open(STATE_FILE, encoding="utf-8")))
    except Exception:
        return set()


def save_seen(seen):
    try:
        json.dump(sorted(seen), open(STATE_FILE, "w", encoding="utf-8"))
    except Exception as e:
        print(f"  [scout] could not save state: {e}")


# ------------------------------------------------------------------------- run

def scan(sources=None):
    events = []
    for name, fn in (sources or SOURCES).items():
        try:
            found = fn()
            print(f"  {name}: {len(found)} events")
            events += found
        except Exception as e:
            print(f"  {name}: FAILED {e}")
    return events


def run(dry=False, min_views=MIN_VIEWS):
    print(f"[scout] scanning (threshold {min_views} views/day)")
    events = scan()
    seen = load_seen()
    fresh = [e for e in events if e["id"] not in seen]
    print(f"[scout] {len(events)} events, {len(fresh)} new")

    hits = []
    for e in fresh:
        v = pageviews(e["performer"])
        e["views"] = v
        if v and v >= min_views:
            hits.append(e)
        time.sleep(0.12)

    hits.sort(key=lambda e: -e["views"])
    for e in hits:
        body = (f"{e['performer']} — {e['views']:,} views/day\n"
                f"{e['title']}\n{e['source']}\n{e['url']}")
        print("\nALERT:\n" + body)
        if not dry:
            try:
                from alerts import _send_telegram, _send, ALERT_EMAIL
                _send_telegram(body)
                if ALERT_EMAIL:
                    _send(ALERT_EMAIL, f"🎟️ {e['performer']} — {e['source']}", body)
            except Exception as ex:
                print(f"  [scout] send failed: {ex}")

    if not dry:
        # Mark everything scanned as seen, not just the hits -- otherwise a
        # non-notable event gets re-checked against Wikipedia on every run.
        save_seen(seen | {e["id"] for e in events})
    print(f"\n[scout] {len(hits)} alert(s) from {len(fresh)} new events")
    return hits


if __name__ == "__main__":
    run(dry="--dry" in sys.argv)
