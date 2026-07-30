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

def resolve_title(name):
    """Ask Wikipedia for the real article title.

    Naive capitalisation silently loses people: title-casing a slug turns
    'mckinnon' into 'Mckinnon', and Wikipedia's article is 'Kate McKinnon', so
    the lookup 404s and a genuinely notable performer is dropped with no error.
    Same for O'Brien, DeGeneres, van der Beek. Let Wikipedia do the matching."""
    url = ("https://en.wikipedia.org/w/api.php?action=opensearch&limit=1"
           "&namespace=0&format=json&search=" + urllib.parse.quote(name))
    try:
        d = json.loads(_get(url, timeout=20))
        return d[1][0] if len(d) > 1 and d[1] else None
    except Exception:
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


SOURCES = {"how_to_academy_us": how_to_academy_us}


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
