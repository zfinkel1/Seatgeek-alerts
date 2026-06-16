"""
SeatGeek price-drop watcher.

Reads a watchlist (Google Sheet published as CSV, or local watchlist.csv), and for
each event that's "due" (per its own check-interval) pulls live listings via the
Bright Data Browser API, checks them against the threshold, and fires email/text
alerts for new qualifying listings (deduped so you're pinged once per ticket).

State (per-event last-checked + already-alerted listing ids) lives in state.json,
committed back by the GitHub Action so throttling + dedup survive between runs.

Watchlist columns (case-insensitive headers):
    url        SeatGeek event URL (required)
    section    section to watch; BLANK = cheapest across all sections
    threshold  number
    type       "$" (flat dollars) or "%" (this much below the avg price)
    every      check interval: 5min|15min|30min|1h|2h|6h|12h|daily  (blank = 30min)
    active     "no"/"false" to pause a row (anything else = on)
"""
import os
import re
import csv
import io
import sys
import json
import time
import urllib.request
from collections import defaultdict
from datetime import date, datetime
from zoneinfo import ZoneInfo

# Windows consoles default to cp1252 and choke on emoji in our log lines;
# force UTF-8 so a print never crashes the run (no-op on Linux/Actions).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from scrape import get_listings
from alerts import send_alert, poll_ignores, mute_key, event_id_from_url

# Persist state (per-event throttle + already-alerted ids) on a Railway VOLUME if
# one is attached, so a redeploy doesn't wipe the dedup memory and re-fire alerts.
STATE_FILE = os.path.join(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "state.json")
SHEET_CSV_URL = os.environ.get("SHEET_CSV_URL")   # published Google Sheet CSV url
LOCAL_WATCHLIST = "watchlist.csv"

_INTERVALS = {
    "5min": 300, "10min": 600, "15min": 900, "30min": 1800,
    "1h": 3600, "2h": 7200, "3h": 10800, "6h": 21600, "12h": 43200,
    "daily": 86400, "24h": 86400,
}
DEFAULT_INTERVAL = 1800  # 30 min if "every" is blank/unrecognized

# Never send more than this many alerts per event per check (cheapest first).
# Prevents a flood when many listings sit under the threshold — you only care
# about the best deals, not every seat. Override via env if you ever want more.
MAX_ALERTS = int(os.environ.get("MAX_ALERTS_PER_CHECK", "3"))

# Resale fee/haircut you lose when you re-sell a flipped ticket (SeatGeek/StubHub).
# Used by "flip" mode to guarantee the net margin after fees. Default 15%.
FLIP_FEE_PCT = float(os.environ.get("FLIP_FEE_PCT", "15"))
# Liquidity gate: a section needs at least this many listings before we trust its
# cheapest asks as the real "going rate". Thin sections can't fake a deal.
FLIP_MIN_LISTINGS = int(os.environ.get("FLIP_MIN_LISTINGS", "5"))
# Compare a listing against its section NEIGHBORHOOD (this many sections on each
# side, by section number) — adjacent sections are comparable seats and give a
# real market, so a couple overpriced section-mates can't fake a deal.
FLIP_ADJ_SECTIONS = int(os.environ.get("FLIP_ADJ_SECTIONS", "4"))
# Single tickets (qty 1) are harder to resell, so require a bigger margin on them
# than the row's normal threshold (pairs). Default 50%.
FLIP_SINGLE_MARGIN = float(os.environ.get("FLIP_SINGLE_MARGIN", "50"))
# Only scrape during active hours (Central time) — no point burning credits at 3am.
ACTIVE_TZ = ZoneInfo("America/Chicago")
ACTIVE_HOUR_START = int(os.environ.get("ACTIVE_HOUR_START", "9"))   # 9am CT
ACTIVE_HOUR_END = int(os.environ.get("ACTIVE_HOUR_END", "20"))      # 8pm CT


def _within_active_hours():
    h = datetime.now(ACTIVE_TZ).hour
    return ACTIVE_HOUR_START <= h < ACTIVE_HOUR_END


def parse_interval(s):
    s = (s or "").strip().lower().replace(" ", "")
    return _INTERVALS.get(s, DEFAULT_INTERVAL)


def _event_date(url):
    """Pull the event date from a SeatGeek URL. Handles both formats:
    concerts ...soldier-field-2026-06-19-5-30-pm/... (YYYY-MM-DD) and
    sports ...cubs-tickets/6-19-2026-chicago-... (M-D-YYYY)."""
    m = re.search(r"(20\d\d)-(\d{1,2})-(\d{1,2})", url)
    if m:
        try:
            return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    m = re.search(r"(?:^|/)(\d{1,2})-(\d{1,2})-(20\d\d)", url)
    if m:
        try:
            return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
        except ValueError:
            pass
    return None


def _section_num(s):
    """Leading section number for numbered seating sections ('130', '230 left',
    '412' -> 130/230/412), so we can group adjacent sections. Returns None for
    named areas ('GA Pit 1', 'Club Box Infield 12') -> those compare by name only."""
    s = (s or "").strip()
    if s and s[0].isdigit():
        m = re.match(r"(\d+)", s)
        if m:
            return int(m.group(1))
    return None


def _section_key(s):
    """(tier, number) for a section. The number lets us group ADJACENT sections;
    the tier keeps different price areas apart so we never value a 'club box
    infield' seat off a bleacher/'108' seat. Most real venues (Wrigley etc.) name
    their sections — 'club box infield 8' must compare to club box infield 6-10,
    not just its own thin listings.
        '108'                              -> ('', 108)
        'club box infield 8'               -> ('club box infield', 8)
        'james hardie catalina club 315 left' -> ('james hardie catalina club left', 315)
        'eero club'                        -> ('eero club', None)   # no number -> by-name
    """
    s = (s or "").strip().lower()
    m = re.search(r"\d+", s)
    if not m:
        return (s, None)
    tier = re.sub(r"\s+", " ", (s[:m.start()] + " " + s[m.end():])).strip()
    return (tier, int(m.group(0)))


def effective_interval(row):
    """Cadence for a row. every=auto ramps by days-to-event:
    >7d -> hourly, <=7d -> 10min (HOT), <=1d (game day) -> 5min, past -> dormant.
    Any other value is a fixed interval (5min/1h/etc)."""
    ev = (row.get("every") or "").strip().lower()
    if ev != "auto":
        return parse_interval(ev)
    d = _event_date(row.get("url", ""))
    if d is None:
        return 3600  # can't read date -> safe hourly default
    days = (d - date.today()).days
    if days < -1:
        return 30 * 86400   # event passed -> effectively off
    if days <= 1:
        return 120          # game day / day before -> 2min (deals move fastest here)
    if days <= 7:
        return 180          # within a week -> 3min (HOT)
    return 3600             # further out -> hourly


def event_id(url):
    m = re.search(r"/(?:concert|event|tickets)?/?(\d{6,})", url) or re.search(r"(\d{6,})", url)
    return m.group(1) if m else url.strip()


def load_watchlist():
    text = None
    if SHEET_CSV_URL:
        try:
            with urllib.request.urlopen(SHEET_CSV_URL, timeout=30) as r:
                text = r.read().decode("utf-8", "replace")
        except Exception as e:
            print(f"[watchlist] sheet fetch failed ({e}); falling back to {LOCAL_WATCHLIST}")
    if text is None and os.path.exists(LOCAL_WATCHLIST):
        text = open(LOCAL_WATCHLIST, encoding="utf-8").read()
    if not text:
        print("[watchlist] no watchlist found")
        return []
    rows = []
    for raw in csv.DictReader(io.StringIO(text)):
        row = { (k or "").strip().lower(): (v or "").strip() for k, v in raw.items() }
        if not row.get("url"):
            continue
        if row.get("active", "").lower() in ("no", "false", "0", "off"):
            continue
        rows.append(row)
    return rows


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {"events": {}}


def save_state(state):
    json.dump(state, open(STATE_FILE, "w"), indent=2)


def evaluate(row, listings):
    """Return (matches, limit, all_candidates). matches = listings at/under the limit."""
    if not listings:
        return [], None, []
    sec = row.get("section", "").strip().lower()
    # section cell can list several sections separated by comma or pipe:
    #   "pit, floor a, floor b"  -> watch any of them in one row
    secs = [s.strip() for s in re.split(r"[,|]", sec) if s.strip()]
    candidates = [L for L in listings if any(s in L["section"].lower() for s in secs)] if secs else listings
    if not candidates:
        return [], None, []
    thr = float(row["threshold"])
    typ = row.get("type", "$").strip().lower()
    if typ.startswith("flip"):
        fee = FLIP_FEE_PCT / 100.0
        margin = thr / 100.0
        single_margin = FLIP_SINGLE_MARGIN / 100.0
        patient = "pat" in typ

        def _buyline(L, R):
            # singles (qty 1) are harder to resell -> demand a bigger margin -> a lower
            # buy price to qualify. Everything else uses the row's normal margin.
            m = single_margin if L.get("qty") == 1 else margin
            return R * (1 - fee) / (1 + m)

        def section_deals(group):
            # A flip = a listing priced margin% below the section's REAL going rate
            # (after fees) — judged on actual asks, not SeatGeek's inflated "value".
            # Reference R = 2nd-cheapest ask (fast: the floor you'd undercut to sell
            # now) or the median (patient). Liquidity-gated so a thin or mostly-
            # overpriced section can't manufacture a fake deal.
            s = sorted(group, key=lambda L: L["price"])
            if len(s) < FLIP_MIN_LISTINGS:
                return []
            if patient:
                ps = [L["price"] for L in s]
                n = len(ps)
                R = ps[n // 2] if n % 2 else (ps[n // 2 - 1] + ps[n // 2]) / 2
            else:
                R = s[1]["price"]
            hits = []
            for L in s:
                if L["price"] <= _buyline(L, R):
                    h = dict(L)
                    h["resale"] = R
                    h["flip_pct"] = (R * (1 - fee) - L["price"]) / L["price"] * 100
                    hits.append(h)
            return [max(hits, key=lambda h: h["flip_pct"])] if hits else []

        if secs:  # explicit section(s) -> treat the watched set as one comparable pool
            deals = section_deals(candidates)
        else:     # whole event -> compare each section vs its NEIGHBORHOOD (+-ADJ
                  # numbered sections). Adjacent sections are comparable seats, so the
                  # "going rate" has real data and a couple overpriced section-mates
                  # can't fake a deal (the false-positive we saw: $1160 looked cheap vs
                  # its own thin section, but section 128 next door was also $1160).
            by_key = defaultdict(list)   # (tier, num) -> listings (numbered + named-with-number)
            by_name = defaultdict(list)  # numberless areas -> own-name pool only
            for L in candidates:
                tier, num = _section_key(L["section"])
                (by_name[tier] if num is None else by_key[(tier, num)]).append(L)

            def neighborhood_deal(own, pool):
                if len(pool) < FLIP_MIN_LISTINGS:
                    return []
                pp = sorted(x["price"] for x in pool)
                if patient:
                    n = len(pp)
                    R = pp[n // 2] if n % 2 else (pp[n // 2 - 1] + pp[n // 2]) / 2
                else:
                    R = pp[1]   # 2nd-cheapest across the neighborhood = corroborated floor
                hits = []
                for L in own:
                    if L["price"] <= _buyline(L, R):
                        h = dict(L)
                        h["resale"] = R
                        h["flip_pct"] = (R * (1 - fee) - L["price"]) / L["price"] * 100
                        hits.append(h)
                return [max(hits, key=lambda h: h["flip_pct"])] if hits else []

            deals = []
            for (tier, num), group in by_key.items():
                pool = []   # this section + its same-tier neighbors within +-ADJ
                for nn in range(num - FLIP_ADJ_SECTIONS, num + FLIP_ADJ_SECTIONS + 1):
                    pool += by_key.get((tier, nn), [])
                deals += neighborhood_deal(group, pool)
            for name, group in by_name.items():   # numberless areas: same-name pool only
                deals += section_deals(group)
        deals.sort(key=lambda L: -L["flip_pct"])  # best flip first
        return deals, None, candidates
    if typ == "%":
        # "deal" mode: fire only when the single CHEAPEST listing sits thr% below
        # the 2nd cheapest — i.e. someone genuinely underpriced it, a real gap at
        # the bottom. Comparing only cheapest-vs-next avoids flagging a whole normal
        # price tier (e.g. the cheap half of a section that's 20% under the premium
        # half). Quiet on a smooth range; fires on a true standout.
        s = sorted(candidates, key=lambda L: L["price"])
        if len(s) >= 2 and s[0]["price"] <= s[1]["price"] * (1 - thr / 100.0):
            limit = s[1]["price"] * (1 - thr / 100.0)
            return [s[0]], limit, candidates
        return [], None, candidates  # cheapest isn't a standout -> stay quiet
    limit = thr
    matches = sorted([L for L in candidates if L["price"] <= limit], key=lambda L: L["price"])
    return matches, limit, candidates


def row_key(row):
    """Per-row state key so multiple rows on the SAME event (different sections /
    thresholds / intervals) each throttle + dedup independently."""
    sec = (row.get("section") or "").strip().lower() or "*"
    typ = (row.get("type") or "$").strip().lower()
    thr = (row.get("threshold") or "").strip()
    return f"{event_id(row['url'])}|{sec}|{typ}{thr}"


def main():
    # Read 'Mute this section' taps every loop (even off-hours) so mutes register
    # promptly; then bail if we're outside active hours.
    state = load_state()
    muted = poll_ignores(state)
    if muted:
        print(f"[ignore] muted {muted} new section(s)")
        save_state(state)
    if not _within_active_hours():
        print(f"[watch] outside {ACTIVE_HOUR_START}:00-{ACTIVE_HOUR_END}:00 CT window — skipping")
        return
    wl = load_watchlist()
    now = time.time()
    print(f"[watch] {len(wl)} active rows")

    scrape_cache = {}  # url -> listings; scrape each event at most once per run

    for row in wl:
        rid = row_key(row)
        est = state["events"].setdefault(rid, {"last": 0, "alerted": []})
        interval = effective_interval(row)
        if now - est["last"] < interval:
            continue  # not due yet
        est["last"] = now
        label = row.get("label") or row.get("section") or rid
        print(f"[check] {label} (every {row.get('every') or '30min'})")
        url = row["url"]
        if url not in scrape_cache:
            scrape_cache[url] = get_listings(url)
        listings = scrape_cache[url]
        if not listings:
            print("   no listings pulled (will retry next due cycle)")
            continue
        matches, limit, candidates = evaluate(row, listings)
        alerted = set(est["alerted"])
        ignored = set(state.get("ignored", []))
        eid = event_id_from_url(url)
        cheapest = min(candidates, key=lambda L: L["price"]) if candidates else None
        if cheapest:
            lim = f"  (buy-line ${limit:.0f})" if limit else ""
            print(f"   cheapest watched: {cheapest['section']} ${cheapest['price']:.0f}{lim} · {len(matches)} deal(s)")
        sent = 0
        for L in matches:
            if mute_key(eid, L["section"]) in ignored:
                est["alerted"].append(L["id"])  # muted section -> record as seen, never alert
                continue
            if L["id"] in alerted:
                continue
            if sent < MAX_ALERTS:
                print(f"   ALERT {L['section']} ${L['price']:.0f}")
                send_alert(label, row["url"], L, limit)
                sent += 1
            # mark seen even when not sent, so a big match-set never floods:
            # you get the cheapest MAX, the rest are recorded silently.
            est["alerted"].append(L["id"])
        if sent:
            print(f"   sent {sent} alert(s) (cheapest first, capped at {MAX_ALERTS})")
        # prune alerted ids no longer present so a re-listing re-alerts
        present = {L["id"] for L in candidates}
        est["alerted"] = [i for i in est["alerted"] if i in present]

    save_state(state)
    print("[watch] done")


if __name__ == "__main__":
    main()
