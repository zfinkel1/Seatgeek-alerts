"""
Pull one event's full history from TicketGenie into a local file.

WHY LOCAL FILES
Billing is per event per started 12-hour window, not per call — so pulling an
event's whole history costs the same as pulling one page of it, and re-pulling
tomorrow costs again. Fetch once, archive, and do all analysis offline. This also
means the research corpus survives the trial ending.

The test key is rate limited (429s), so every call backs off and retries rather
than dropping data on the floor mid-pull.

    python tg_pull.py <platform> <nativeId> [label]
"""
import os
import sys
import gzip
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.environ.get("TICKETGENIE_BASE", "https://api.ticketgenie.io/api/v1")
API_KEY = os.environ.get("TICKETGENIE_API_KEY")
OUT_DIR = os.environ.get("TG_CORPUS_DIR", "corpus")

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def call(method, path, body=None, tries=6):
    """One request, with backoff on 429/5xx. Returns parsed JSON or raises."""
    url = BASE + path
    delay = 2.0
    for attempt in range(tries):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"ApiKey {API_KEY}")
        if data:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body_txt = ""
            try:
                body_txt = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            if e.code in (429, 500, 502, 503, 504) and attempt < tries - 1:
                print(f"    {e.code}; retrying in {delay:.0f}s")
                time.sleep(delay)
                delay = min(delay * 2, 60)
                continue
            raise RuntimeError(f"HTTP {e.code}: {body_txt[:300]}") from None
    raise RuntimeError("retries exhausted")


def paginate(path, body, items_key, limit, max_pages=40):
    """Keyset pagination. Within one 12h billing window the extra pages are free,
    so pull everything rather than sampling."""
    body = dict(body)
    body.setdefault("page", {})["limit"] = limit
    out, pages = [], 0
    while True:
        payload = call("POST", path, body)
        rows = payload.get(items_key) or []
        out.extend(rows)
        pages += 1
        cur = (payload.get("page") or {}).get("nextCursor")
        if not cur or not rows or pages >= max_pages:
            if cur and rows and pages >= max_pages:
                print(f"    stopped at max_pages={max_pages}; MORE AVAILABLE")
            return out, payload


def event_meta(native):
    """Event date, venue, capacity. Without `dateUtc` there is no days-to-event,
    and every rule we have is keyed on distance to the event — so this is not
    optional metadata, it's the x-axis."""
    try:
        payload = call("POST", "/events", {"nativeIds": [native],
                                           "meta": {"limit": 1}})
    except Exception as e:
        print(f"    [meta] lookup failed: {e}")
        return {}
    events = payload.get("events") or payload.get("data") or []
    if not events:
        return {}
    ev = events[0]
    disp = ev.get("displaySummary") or {}
    venue = disp.get("venue") or {}
    return {
        "event_id": ev.get("id"),
        "name": ev.get("name"),
        "date_utc": ev.get("dateUtc"),
        "venue": venue.get("name"),
        "city": venue.get("city"),
        "state": venue.get("state"),
        "capacity": venue.get("capacity") or disp.get("eventTotalCapacity"),
        "genre": (disp.get("classification") or {}).get("fullGenre"),
    }


def pull_event(platform, native_id, label=None, snapshot_interval="6h"):
    native = {"id": str(native_id), "platform": platform}
    now = datetime.now(timezone.utc)
    span = {"from": (now - timedelta(days=400)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "to": now.strftime("%Y-%m-%dT%H:%M:%SZ")}

    print(f"[pull] {platform}/{native_id} {label or ''}")
    meta = event_meta(native)
    if meta:
        print(f"    {meta.get('name')} | {meta.get('date_utc')} | "
              f"{meta.get('venue')}, {meta.get('city')}")

    print("  sales…")
    sales, spay = paginate("/events/sales/timeseries",
                           {"nativeId": native, "dateRange": span,
                            "sort": {"field": "DATE", "order": "ASC"}},
                           "sales", 1000)
    print(f"    {len(sales)} sales  range={spay.get('availableDateRange')}")

    print(f"  snapshot index (interval={snapshot_interval})…")
    snaps, npay = paginate("/events/listings/timeseries/snapshots",
                           {"nativeId": native, "dateRange": span,
                            "interval": snapshot_interval,
                            "sort": {"field": "TIMESTAMP", "order": "ASC"}},
                           "snapshots", 1000)
    print(f"    {len(snaps)} snapshots  range={npay.get('availableDateRange')}")

    # Ladder detail per snapshot. Billed per event per 12h window, so pulling
    # every snapshot for this event costs the same as pulling one.
    ladders = []
    for i, s in enumerate(snaps):
        ts = s.get("timestamp")
        if not ts:
            continue
        rows, _ = paginate("/events/listings/timeseries/snapshots/details",
                           {"nativeId": native, "timestamp": ts,
                            "interval": snapshot_interval},
                           "listings", 2000, max_pages=6)
        ladders.append({"t": ts, "listings": rows})
        if (i + 1) % 10 == 0 or i + 1 == len(snaps):
            print(f"    ladders {i+1}/{len(snaps)}")

    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, f"{platform}-{native_id}.json.gz")
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump({
            "native": native,
            "label": label,
            "meta": meta,
            "pulled_at": now.isoformat(),
            "sales_range": spay.get("availableDateRange"),
            "snapshots_range": npay.get("availableDateRange"),
            "sales": sales,
            "ladders": ladders,
        }, f)
    print(f"  saved {path}  ({os.path.getsize(path)/1e6:.2f} MB)")
    return path


def main():
    if not API_KEY:
        print("ERROR: TICKETGENIE_API_KEY not set")
        return 1
    if len(sys.argv) < 3:
        print(__doc__)
        return 1
    pull_event(sys.argv[1], sys.argv[2],
               " ".join(sys.argv[3:]) if len(sys.argv) > 3 else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
