"""
Watchlist maintenance:
  1. Deactivate any event whose date is in the past (active -> no).
  2. Add every upcoming SeatGeek event at a venue (by venue id), as flip rows.

Usage:
  python add_venue.py 136            # United Center = 136 (add + deactivate passed)
  python add_venue.py 136 --no-add   # only deactivate passed, don't add
Uses the public SeatGeek events API (no Scrapfly credits — this is just the
schedule, not protected listings). New rows: threshold=40, type=flip, every=auto,
active=yes. Skips events already in the watchlist (by event id).
"""
import csv, json, re, sys, urllib.request
from datetime import date

CID = "MTY2MnwxMzgzMzIwMTU4"  # SeatGeek stable web client id
WATCHLIST = "watchlist.csv"
FIELDS = ["url", "section", "threshold", "type", "every", "label", "active"]


def event_id(url):
    m = re.search(r"/(\d{6,})(?:[/?#]|$)", url or "") or re.search(r"(\d{6,})", url or "")
    return m.group(1) if m else None


def url_date(url):
    # SeatGeek URLs carry the date two ways: ISO YYYY-MM-DD (concerts) or
    # M-D-YYYY (sports). Match watch.py so deactivation works for both.
    m = re.search(r"(20\d\d)-(\d{1,2})-(\d{1,2})", url or "")
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r"(?:^|[/-])(\d{1,2})-(\d{1,2})-(20\d\d)", url or "")
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


def fetch_venue_events(vid):
    u = (f"https://api.seatgeek.com/2/events?venue.id={vid}"
         f"&per_page=100&sort=datetime_local.asc&client_id={CID}")
    req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"})
    data = json.loads(urllib.request.urlopen(req, timeout=25).read())
    out = []
    for e in data.get("events", []):
        url = e.get("url")
        dt = (e.get("datetime_local") or "")[:10]
        if not url or not dt:
            continue
        title = re.sub(r"[,\n]", " ", (e.get("title") or "")).strip()[:24]
        try:
            d = date(*map(int, dt.split("-")))
        except ValueError:
            continue
        label = f"{title} {d.month}/{d.day}"
        out.append({"url": url, "date": d, "label": label})
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    vid = sys.argv[1]
    do_add = "--no-add" not in sys.argv
    today = date.today()

    with open(WATCHLIST, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    have = {event_id(r["url"]) for r in rows}

    # 1. Deactivate passed events
    deact = 0
    for r in rows:
        d = url_date(r["url"])
        if d and d < today and (r.get("active") or "").strip().lower() == "yes":
            r["active"] = "no"; deact += 1

    # 2. Add the venue's upcoming slate (skip ones already present)
    added = 0
    if do_add:
        for ev in fetch_venue_events(vid):
            if event_id(ev["url"]) in have or ev["date"] < today:
                continue
            rows.append({"url": ev["url"], "section": "", "threshold": "40",
                         "type": "flip", "every": "auto", "label": ev["label"], "active": "yes"})
            have.add(event_id(ev["url"])); added += 1

    with open(WATCHLIST, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader(); w.writerows(rows)

    active = sum(1 for r in rows if (r.get("active") or "").lower() == "yes")
    print(f"Deactivated {deact} passed events.")
    print(f"Added {added} new events from venue {vid}.")
    print(f"Watchlist now: {len(rows)} rows, {active} active.")


if __name__ == "__main__":
    main()
