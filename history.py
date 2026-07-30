"""
Append-only price history — the training set the watcher was throwing away.

Every cycle we already pull the full section-level ladder from five platforms and
then discard everything that doesn't trip an alert. state.json keeps only throttle
timestamps and dedup signatures. That means months of running produced no price
history at all, and history is the one thing you cannot backfill: nobody sells you
last Tuesday's ask ladder for a specific event.

Recording is FREE — the listings are already in memory when this is called. It adds
no scrape, no proxy credit, no API cost. That is the whole argument for doing it now
rather than after a vendor decision.

FORMAT
One gzipped JSONL file per UTC day. Each line is one snapshot of one event:

    {"t": 1781623852, "url": "...", "src": "seatgeek", "n": 412,
     "cols": ["section","price","qty","row","id","value","score","flags"],
     "rows": [["117", 88.0, 2, "12", "abc", 121.0, 87, ""], ...]}

Rows are arrays, not objects, because repeating eight keys across ~500 listings per
snapshot roughly triples the file size for no added information. `cols` travels with
each line so the reader stays correct if the schema ever changes.

Writes never raise into the caller — a full disk or a locked file must not take the
watcher down. A failed write is logged once and dropped.
"""
import os
import gzip
import json
import time
from datetime import datetime, timezone

# Sit next to state.json so a Railway volume persists history across redeploys.
# Without a volume this still works, it just doesn't survive a restart.
HISTORY_DIR = os.path.join(
    os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "history"
)
# Set HISTORY_ENABLED=0 to turn recording off without touching the watcher.
ENABLED = os.environ.get("HISTORY_ENABLED", "1") != "0"

COLS = ["section", "price", "qty", "row", "id", "value", "score", "flags"]

_warned = False


def _source(url):
    """Which platform a URL came from — so a mixed history file can be split back
    apart per platform without re-parsing URLs at analysis time."""
    u = (url or "").lower()
    for host, name in (
        ("stubhub.com", "stubhub"),
        ("gametime.co", "gametime"),
        ("vividseats.com", "vividseats"),
        ("ticketexchangebyticketmaster.com", "ticketexchange"),
        ("seatgeek.com", "seatgeek"),
    ):
        if host in u:
            return name
    return "unknown"


def _path(ts):
    day = datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")
    return os.path.join(HISTORY_DIR, f"{day}.jsonl.gz")


def record(url, listings, ts=None):
    """Append one snapshot. Returns True if written.

    Called once per event per scrape, straight from the listings already in hand."""
    global _warned
    if not (ENABLED and listings):
        return False
    ts = int(ts or time.time())
    try:
        os.makedirs(HISTORY_DIR, exist_ok=True)
        rows = []
        for L in listings:
            rows.append([
                L.get("section"),
                L.get("price"),
                L.get("qty"),
                L.get("row"),
                L.get("id"),
                L.get("value"),
                L.get("score"),
                L.get("flags") or "",
            ])
        line = json.dumps({
            "t": ts,
            "url": url,
            "src": _source(url),
            "n": len(rows),
            "cols": COLS,
            "rows": rows,
        }, separators=(",", ":"))
        # Append mode on a gzip file writes a second gzip member. That is valid,
        # and gzip.open/zcat read the concatenation transparently — which is what
        # makes cheap per-snapshot appends possible without rewriting the file.
        with gzip.open(_path(ts), "at", encoding="utf-8") as f:
            f.write(line + "\n")
        return True
    except Exception as e:
        if not _warned:
            print(f"  [history] recording disabled for this run: {e}")
            _warned = True
        return False


def iter_snapshots(path):
    """Yield snapshot dicts from one history file, listings re-expanded to the
    same dict shape the scrapers return — so analysis code and the live flip
    engine consume identical structures."""
    with gzip.open(path, "rt", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                snap = json.loads(line)
            except Exception:
                continue  # skip a torn line rather than abort the whole file
            cols = snap.get("cols") or COLS
            snap["listings"] = [dict(zip(cols, r)) for r in snap.get("rows", ())]
            snap.pop("rows", None)
            yield snap


def files(directory=None):
    """History files oldest-first."""
    d = directory or HISTORY_DIR
    if not os.path.isdir(d):
        return []
    return sorted(
        os.path.join(d, n) for n in os.listdir(d) if n.endswith(".jsonl.gz")
    )


def stats(directory=None):
    """Summary of what's been captured — snapshots, events, listings, span, size.
    Run `python history.py` to see whether capture is actually working."""
    total_snaps = total_listings = 0
    events = set()
    first = last = None
    paths = files(directory)
    for p in paths:
        for snap in iter_snapshots(p):
            total_snaps += 1
            total_listings += snap.get("n") or 0
            events.add(snap.get("url"))
            t = snap.get("t")
            if t:
                first = t if first is None else min(first, t)
                last = t if last is None else max(last, t)
    size = sum(os.path.getsize(p) for p in paths)
    return {
        "files": len(paths),
        "snapshots": total_snaps,
        "events": len(events),
        "listings": total_listings,
        "first": datetime.fromtimestamp(first, timezone.utc).isoformat() if first else None,
        "last": datetime.fromtimestamp(last, timezone.utc).isoformat() if last else None,
        "bytes_on_disk": size,
        "days_covered": round((last - first) / 86400, 2) if (first and last) else 0,
    }


if __name__ == "__main__":
    s = stats()
    print(f"history dir: {os.path.abspath(HISTORY_DIR)}")
    if not s["files"]:
        print("no history captured yet: is the worker running with HISTORY_ENABLED=1?")
    else:
        mb = s["bytes_on_disk"] / 1e6
        print(f"  files:      {s['files']}")
        print(f"  snapshots:  {s['snapshots']:,}")
        print(f"  events:     {s['events']:,}")
        print(f"  listings:   {s['listings']:,}")
        print(f"  span:       {s['first']} -> {s['last']}  ({s['days_covered']} days)")
        print(f"  on disk:    {mb:.1f} MB", end="")
        if s["days_covered"] >= 1:
            print(f"  (~{mb / s['days_covered']:.1f} MB/day)")
        else:
            print()
