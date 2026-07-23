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

from scrape import get_listings as _seatgeek_listings
from stubhub import get_listings as _stubhub_listings
from gametime import get_listings as _gametime_listings
from vividseats import get_listings as _vivid_listings
from alerts import send_alert, poll_ignores, mute_key, event_id_from_url


def get_listings(url):
    """Route to the right scraper by the event URL's site. Every scraper returns
    the same normalized shape {section, price, qty, row, id, value, score}, so the
    flip engine and everything downstream are source-agnostic."""
    u = (url or "").lower()
    if "stubhub.com" in u:
        return _stubhub_listings(url)
    if "gametime.co" in u:
        return _gametime_listings(url)
    if "vividseats.com" in u:
        return _vivid_listings(url)
    return _seatgeek_listings(url)

# Persist state (per-event throttle + already-alerted ids) on a Railway VOLUME if
# one is attached, so a redeploy doesn't wipe the dedup memory and re-fire alerts.
STATE_FILE = os.path.join(os.environ.get("RAILWAY_VOLUME_MOUNT_PATH", "."), "state.json")
SHEET_CSV_URL = os.environ.get("SHEET_CSV_URL")   # published Google Sheet CSV url
LOCAL_WATCHLIST = "watchlist.csv"

_INTERVALS = {
    # Sub-5min tiers are EXPENSIVE (each check = a Scrapfly render, ~25 credits).
    # Reserve them for a hot event you're actively hunting — at 3min an event
    # burns ~20x what a 1h row does. Unknown values fall back to DEFAULT_INTERVAL.
    "1min": 60, "2min": 120, "3min": 180,
    "5min": 300, "10min": 600, "15min": 900, "30min": 1800,
    "1h": 3600, "2h": 7200, "3h": 10800, "6h": 21600, "12h": 43200,
    "daily": 86400, "24h": 86400,
}
DEFAULT_INTERVAL = 1800  # 30 min if "every" is blank/unrecognized

# Never send more than this many alerts per event per check (cheapest first).
# Prevents a flood when many listings sit under the threshold — you only care
# about the best deals, not every seat. Override via env if you ever want more.
MAX_ALERTS = int(os.environ.get("MAX_ALERTS_PER_CHECK", "3"))
# Buy watches (a "$" row that names sections) want to see EVERY qualifying seat in
# those sections, not just the cheapest few — so they get a much higher per-check
# cap. Still bounded so a section that dumps a huge block can't flood the inbox.
BUY_MAX_ALERTS = int(os.environ.get("BUY_MAX_ALERTS", "25"))
# Don't re-alert the SAME deal (event+section+row+price) within this many hours,
# even if it flickers out of the scrape and back. StubHub re-keys its listing id
# as the cheapest offering in a section rotates, which spammed 4 emails for one
# $37 listing. A genuine price DROP makes a new signature and alerts immediately;
# mute a section to silence it entirely.
ALERT_COOLDOWN_HRS = float(os.environ.get("ALERT_COOLDOWN_HRS", "12"))

# Resale fee/haircut you lose when you re-sell a flipped ticket (SeatGeek/StubHub).
# Used by "flip" mode to guarantee the net margin after fees. Default 15%.
FLIP_FEE_PCT = float(os.environ.get("FLIP_FEE_PCT", "15"))
# Liquidity gate: a section needs at least this many listings before we trust its
# cheapest asks as the real "going rate". Thin sections can't fake a deal.
FLIP_MIN_LISTINGS = int(os.environ.get("FLIP_MIN_LISTINGS", "5"))
# Compare a listing against its section NEIGHBORHOOD (this many sections on each
# side, by section number) — adjacent sections are comparable seats and give a
# real market, so a couple overpriced section-mates can't fake a deal.
FLIP_ADJ_SECTIONS = int(os.environ.get("FLIP_ADJ_SECTIONS", "3"))  # +-3 (was 5): five sections crossed real price tiers around arena corners — muddy comps
# A real "going rate" needs DEPTH: at least FLIP_DEPTH listings clustered within
# FLIP_BAND of a price. A lone cheap ask sitting under a sparse ladder of pricey
# ones (thin premium sections) has no such cluster -> not a deal, skip it.
FLIP_DEPTH = int(os.environ.get("FLIP_DEPTH", "3"))
FLIP_BAND = float(os.environ.get("FLIP_BAND", "15")) / 100.0
# Sanity ceiling: a real flip's resale is never a huge multiple of the buy. When
# the "going rate" comes back many times the buy price it's a junk/placeholder
# listing polluting the comps (sellers park tickets at $50,111 etc so they show
# but never sell), NOT a deal. Reject any flip whose resale exceeds buy x this.
MAX_RESALE_MULT = float(os.environ.get("MAX_RESALE_MULT", "6"))
# SeatGeek's own per-listing "value" estimate (dq.ev, already scraped) is a FREE
# second opinion on our going rate. If OUR computed resale R wildly exceeds what
# SeatGeek itself thinks the seat is worth, the comp pool is skewed (sparse or
# premium-polluted) — that's a comp error, not a deal. R must stay within
# value * this multiple whenever the listing carries a value estimate.
RESALE_VS_VALUE_MULT = float(os.environ.get("RESALE_VS_VALUE_MULT", "1.5"))
# Single tickets (qty 1) are harder to resell, so require a bigger margin on them
# than the row's normal threshold (pairs). Back to 100 (2026-07-06: founder wants
# only BETTER flips — the 75 volume test is over).
FLIP_SINGLE_MARGIN = float(os.environ.get("FLIP_SINGLE_MARGIN", "75"))
# Minimum NET profit (after the resale fee) for a flip to alert. Percentage alone
# lets $15-profit flips on cheap tickets through; a real flip must also be worth
# the effort in dollars. Set 0 to disable.
FLIP_MIN_NET = float(os.environ.get("FLIP_MIN_NET", "0"))  # off — founder wants volume; comps quality is the focus
# NFL stadiums are huge (60k+) with dozens of thinly-listed sections, so the tight-
# venue defaults above fired off fake floors built from a handful of asks. NFL needs
# MORE comparables, drawn from TRULY adjacent seats, before any deal qualifies:
# require 6 listings within +-2 sections of the target. Applied only to /nfl/ event
# URLs — baseball/concerts keep the defaults that are already dialed in.
NFL_FLIP_MIN_LISTINGS = int(os.environ.get("NFL_FLIP_MIN_LISTINGS", "6"))
NFL_FLIP_DEPTH = int(os.environ.get("NFL_FLIP_DEPTH", "3"))
NFL_FLIP_ADJ_SECTIONS = int(os.environ.get("NFL_FLIP_ADJ_SECTIONS", "2"))
# Buy-price ceiling — BASEBALL ONLY. 0 = DISABLED (no cap) — premium high-dollar
# MLB flips can fire. (Was $400 to stay on cheaper/faster seats; founder removed it
# 2026-06-26 to capture the big-margin premium plays.) Set a $ value to re-enable.
FLIP_MAX_BUY = float(os.environ.get("FLIP_MAX_BUY", "0"))
# Sections to NEVER alert on (founder excluded — the field-level club/bullpen
# boxes that produced bad buys). Matched by the section NUMBER via _section_key,
# so "club box infield 8", "bullpen box 6", "makers mark barrel room 27" all
# match 8/6/27 — but "108"/"208" do NOT (that's the whole number, not a digit).
# Override via EXCLUDE_SECTIONS (comma-separated numbers; empty = exclude none).
EXCLUDE_SECTION_NUMS = {int(n) for n in re.findall(r"\d+", os.environ.get("EXCLUDE_SECTIONS", "3,4,5,6,7,8,27,28,29,30,31,32"))}
# Section NAMES to NEVER alert on: GA floor/pit/sro (standing-room) + vip/suite/
# club/lounge/hospitality (premium hospitality). All flip unpredictably. Matched
# as whole words in the section string, so "Floor A", "GA Pit", "VIP Row", "Luxury
# Suite", "Club Box", "SRO" all drop, but "Capitol" (no whole-word match) stays.
# NOTE: "club" cuts ALL club sections (club level too), not just club boxes.
# Override via EXCLUDE_SECTION_NAMES (comma-separated; empty = exclude none).
EXCLUDE_SECTION_NAMES = [s.strip().lower() for s in os.environ.get("EXCLUDE_SECTION_NAMES", "floor,pit,sro,vip,suite,suites,club,lounge,hospitality").split(",") if s.strip()]
_EXCLUDE_NAME_RE = re.compile(r"\b(" + "|".join(re.escape(n) for n in EXCLUDE_SECTION_NAMES) + r")\b", re.I) if EXCLUDE_SECTION_NAMES else None
# Bad-view seats: side/rear/behind stage, obstructed, limited/partial view.
# They're cheap for a reason and resell badly — never alert on them. Matched
# against BOTH the listing's disclosure flags (SeatGeek sends codes like
# "side_stage"/"obstructed_view" — captured as `flags` by scrape.py) and the
# section name (StubHub has no per-listing flags, but its side-stage sections
# are usually just NAMED that). Override via EXCLUDE_VIEW_RE; empty disables.
_view_pat = os.environ.get(
    "EXCLUDE_VIEW_RE",
    r"side.{0,3}stage|rear.{0,3}stage|behind.{0,3}stage|back.{0,3}of.{0,3}stage|obstruct|limited.{0,3}view|partial.{0,3}view",
)
_EXCLUDE_VIEW_RE = re.compile(_view_pat, re.I) if _view_pat else None
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
    m = re.search(r"(?:^|[/-])(\d{1,2})-(\d{1,2})-(20\d\d)", url)
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
        'philadelphia insurance club g'    -> ('philadelphia insurance club', 7)  # letter index
        'floor a'                          -> ('floor', 1)
    """
    s = (s or "").strip().lower()
    m = re.search(r"\d+", s)
    if not m:
        # No number — but lettered sections (Club C/D/E/F/G, Floor A/B) use a
        # TRAILING single letter as their index. Treat it like a number (a=1..z=26)
        # so adjacent lettered sections in the same area pool as comparable seats
        # instead of each letter being its own isolated silo.
        lm = re.search(r"(?:^|\s)([a-z])\s*$", s)
        if lm:
            return (s[:lm.start(1)].strip(), ord(lm.group(1)) - ord("a") + 1)
        return (s, None)
    tier = re.sub(r"\s+", " ", (s[:m.start()] + " " + s[m.end():])).strip()
    return (tier, int(m.group(0)))


def _going_rate(prices, depth=None):
    """The real resale floor: the cheapest price that has DEPTH — at least
    `depth` listings within +FLIP_BAND of it. A lone cheap ask sitting under a
    sparse ladder of pricey ones (thin premium sections like 'bullpen box 6':
    283 then 597/701/874...) has NO such cluster, so we return None and skip it —
    that's a wide-spread section, not a going rate you could resell into.
    `depth` defaults to FLIP_DEPTH; NFL passes a higher bar for more comparables."""
    depth = FLIP_DEPTH if depth is None else depth
    ps = sorted(prices)
    for p in ps:
        hi = p * (1 + FLIP_BAND)
        if sum(1 for q in ps if p <= q <= hi) >= depth:
            return p
    return None


def section_market_rate(listings, section, patient=False, adj=None, min_listings=None, depth=None):
    """The going rate for `section`'s neighborhood within ONE platform's listings —
    used to sanity-check the OTHER platform's resale reference. Matches by section
    NUMBER (+-adj), ignoring tier-name strings, because the two sites label the
    same seats differently ('446' vs 'Section 446'). Returns None for numberless
    areas or a pool too thin to trust. Mirrors _going_rate / the neighborhood pool.
    adj/min_listings/depth default to the globals; NFL passes tighter values."""
    adj = FLIP_ADJ_SECTIONS if adj is None else adj
    min_listings = FLIP_MIN_LISTINGS if min_listings is None else min_listings
    if not listings:
        return None
    _, num = _section_key(section)
    if num is None:
        return None  # numberless area -> can't reliably cross-match across platforms
    pool = []
    for L in listings:
        n = _section_key(L["section"])[1]
        if n is not None and abs(n - num) <= adj:
            pool.append(L)
    if len(pool) < min_listings:
        return None
    pp = sorted(x["price"] for x in pool)
    if patient:
        n = len(pp)
        return pp[n // 2] if n % 2 else (pp[n // 2 - 1] + pp[n // 2]) / 2
    return _going_rate(pp, depth)


def effective_interval(row):
    """Cadence for a row. every=auto ramps by days-to-event:
    <=7d (game day + week of) -> 10min, 8-30d -> 3h, 31+d -> 8h, past -> dormant.
    10min in the hot window is frequent enough to catch a price drop but slow
    enough to keep the credit bill sane across a big watchlist; far-out slows way
    down because distant prices barely move. Any other 'every' value is a fixed
    interval."""
    ev = (row.get("every") or "").strip().lower()
    if ev not in ("auto", "auto+"):
        return parse_interval(ev)
    d = _event_date(row.get("url", ""))
    if d is None:
        return 3600  # can't read date -> safe hourly default
    days = (d - date.today()).days
    if days < -1:
        return 30 * 86400   # event passed -> effectively off
    if ev == "auto+":
        # Priority tier (founder: Cubs) — same ramp, ~2x hotter at every distance.
        if days <= 7:
            return 300      # game week -> 5min
        if days <= 30:
            return 3600     # 8-30 days -> hourly
        return 3 * 3600     # 31+ days -> every 3h
    if days <= 7:
        return 600          # game day + week of -> 10min
    if days <= 30:
        return 3 * 3600     # 8-30 days -> every 3h
    return 8 * 3600         # 31+ days -> every 8h


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


def evaluate(row, listings, mirror_listings=None):
    """Return (matches, limit, all_candidates). matches = listings at/under the limit.
    mirror_listings = the SAME event's listings on the OTHER platform (StubHub<->
    SeatGeek), used to cross-check flip resale prices against the real market."""
    if not listings:
        return [], None, []
    sec = row.get("section", "").strip().lower()
    # section cell can list several sections separated by comma or pipe:
    #   "pit, floor a, floor b"  -> watch any of them in one row
    secs = [s.strip() for s in re.split(r"[,|]", sec) if s.strip()]
    # A section spec ending in "*" is a PREFIX match ("a*" = any section whose
    # name starts with "a", e.g. the 1914 Club's A1/A5/A14 sections). Otherwise
    # it's the usual substring match. Lets us target a whole named tier without
    # listing every section number.
    def _sec_hit(sect):
        low = sect.lower().strip()
        return any(low.startswith(s[:-1].strip()) if s.endswith("*") else (s in low) for s in secs)
    candidates = [L for L in listings if _sec_hit(L["section"])] if secs else listings
    # When a row NAMES exact sections and isn't a flip row, it's a personal BUY
    # watch — the user picked those sections on purpose, so honor them verbatim and
    # skip the flip-engine exclusions below (which exist to keep the whole-event
    # flip scan out of premium/club/bad-view seats). Without this, a wanted "United
    # Club" or side section would be silently dropped and never alert.
    _typ_raw = row.get("type", "$").strip().lower()
    explicit_pick = bool(secs) and not _typ_raw.startswith("flip")
    # Drop excluded sections entirely — they never alert AND never count toward a
    # neighbor's going rate, so a bad premium box can't skew nearby sections either.
    if EXCLUDE_SECTION_NUMS and not explicit_pick:
        candidates = [L for L in candidates if _section_key(L["section"])[1] not in EXCLUDE_SECTION_NUMS]
    if _EXCLUDE_NAME_RE and not explicit_pick:
        candidates = [L for L in candidates if not _EXCLUDE_NAME_RE.search(L.get("section") or "")]
    # Bad-view seats never alert (they DO stay in the comps pool — a cheap
    # side-stage ask can only pull a section's going rate DOWN, which is the
    # conservative direction for a buy decision).
    if _EXCLUDE_VIEW_RE and not explicit_pick:
        candidates = [L for L in candidates
                      if not _EXCLUDE_VIEW_RE.search(f"{L.get('section') or ''} {L.get('flags') or ''}")]
    if not candidates:
        return [], None, []
    thr = float(row["threshold"])
    typ = row.get("type", "$").strip().lower()
    if typ.startswith("flip"):
        fee = FLIP_FEE_PCT / 100.0
        margin = thr / 100.0
        single_margin = FLIP_SINGLE_MARGIN / 100.0
        patient = "pat" in typ
        # NFL needs more comparables from truly-adjacent seats — use the NFL gates
        # for /nfl/ events, the dialed-in defaults for everything else.
        is_nfl = "/nfl/" in (row.get("url") or "").lower()
        min_listings = NFL_FLIP_MIN_LISTINGS if is_nfl else FLIP_MIN_LISTINGS
        depth = NFL_FLIP_DEPTH if is_nfl else FLIP_DEPTH
        adj = NFL_FLIP_ADJ_SECTIONS if is_nfl else FLIP_ADJ_SECTIONS

        # Same-level reality check. The tight neighborhood can call a lone cheap
        # listing a deal vs a pricey cluster in ITS section, while the whole level is
        # full of cheaper comparable seats in adjacent sections (the C141 $646-vs-
        # $1529 false alert: $646 was market price — the level was packed with
        # $462-691 asks). Group every candidate by section TIER (the non-number
        # prefix, so all "c1xx" field seats share a level) and refuse to call a
        # listing a flip when `depth`+ comparable same-tier seats already sit at/under
        # its price — you could never resell high when cheaper equivalents are there.
        tier_prices = defaultdict(list)
        for L in candidates:
            tier_prices[_section_key(L["section"])[0]].append(L["price"])
        for _t in tier_prices:
            tier_prices[_t].sort()

        def _scarce_enough(L):
            tier = _section_key(L["section"])[0]
            ceiling = L["price"] * 1.05
            at_or_below = sum(1 for p in tier_prices.get(tier, ()) if p <= ceiling)
            return at_or_below <= depth   # itself + up to (depth-1) others; more = not scarce

        def _value_ok(L, R):
            # Cross-check our going rate against SeatGeek's own value estimate
            # for the listing — no estimate means no check.
            v = L.get("value")
            if not v or v <= 0:
                return True
            return R <= v * RESALE_VS_VALUE_MULT

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
            if len(s) < min_listings:
                return []
            if patient:
                ps = [L["price"] for L in s]
                n = len(ps)
                R = ps[n // 2] if n % 2 else (ps[n // 2 - 1] + ps[n // 2]) / 2
            else:
                R = _going_rate([L["price"] for L in s], depth)
                if R is None:
                    return []   # no clustered floor -> thin/spread section, not a deal
            hits = []
            for L in s:
                if L["price"] <= _buyline(L, R) and _scarce_enough(L) and R <= L["price"] * MAX_RESALE_MULT and (R * (1 - fee) - L["price"]) >= FLIP_MIN_NET and _value_ok(L, R):
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
                if len(pool) < min_listings:
                    return []
                pp = sorted(x["price"] for x in pool)
                if patient:
                    n = len(pp)
                    R = pp[n // 2] if n % 2 else (pp[n // 2 - 1] + pp[n // 2]) / 2
                else:
                    R = _going_rate(pp, depth)   # clustered floor across the neighborhood
                    if R is None:
                        return []          # no real cluster -> not a deal
                hits = []
                for L in own:
                    if L["price"] <= _buyline(L, R) and _scarce_enough(L) and R <= L["price"] * MAX_RESALE_MULT and (R * (1 - fee) - L["price"]) >= FLIP_MIN_NET and _value_ok(L, R):
                        h = dict(L)
                        h["resale"] = R
                        h["flip_pct"] = (R * (1 - fee) - L["price"]) / L["price"] * 100
                        hits.append(h)
                return [max(hits, key=lambda h: h["flip_pct"])] if hits else []

            deals = []
            for (tier, num), group in by_key.items():
                pool = []   # this section + its same-tier neighbors within +-adj
                for nn in range(num - adj, num + adj + 1):
                    pool += by_key.get((tier, nn), [])
                deals += neighborhood_deal(group, pool)
            for name, group in by_name.items():   # numberless areas: same-name pool only
                deals += section_deals(group)
        # buy-price ceiling — BASEBALL ONLY (concerts have a higher floor, e.g.
        # Morgan Wallen get-in >$400, so a cap would kill every concert flip).
        if FLIP_MAX_BUY and "/mlb/" in (row.get("url") or "").lower():
            deals = [d for d in deals if d["price"] <= FLIP_MAX_BUY]
        # Cross-platform reality check. A platform's resale "going rate" can be a
        # wall of overpriced, NON-clearing inventory: StubHub showed a $454 cluster
        # in section 446 while SeatGeek cleared the same seats at ~$262, so a $259
        # StubHub buy looked like a +49% flip but really nets a LOSS. Cap each deal's
        # resale by the same section's going rate on the mirror platform and re-test
        # the buy-line — a flip survives only if it clears against the CHEAPER of the
        # two markets (the price a buyer would actually pay).
        if mirror_listings:
            kept = []
            for d in deals:
                mR = section_market_rate(mirror_listings, d["section"], patient, adj, min_listings, depth)
                if mR is not None and mR < d["resale"]:
                    m = single_margin if d.get("qty") == 1 else margin
                    if d["price"] > mR * (1 - fee) / (1 + m):
                        print(f"   x cross-platform: {d['section']} ${d['price']:.0f} — "
                              f"mirror going rate ${mR:.0f} (this platform claimed ${d['resale']:.0f}); skip")
                        continue
                    d["resale"] = mR
                    d["flip_pct"] = (mR * (1 - fee) - d["price"]) / d["price"] * 100
                kept.append(d)
            deals = kept
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


def deal_sig(eid, L):
    """Stable identity for a deal so a platform's listing-id churn (StubHub re-keys
    ticketClassId as the cheapest offering rotates) can't re-alert the same seat at
    the same price. Price IS included, so a genuine drop makes a new sig + re-alerts."""
    sec = re.sub(r"\s+", " ", str(L.get("section") or "")).strip().lower()
    rw = re.sub(r"\s+", " ", str(L.get("row") or "")).strip().lower()
    try:
        price = round(float(L.get("price") or 0))
    except (TypeError, ValueError):
        price = 0
    return f"{eid}|{sec}|{rw}|{price}"


# Global kill-switch. True = the worker idles (no scraping, no Scrapfly credits,
# no alerts). Flip via env SEATGEEK_PAUSED=1 to pause again.
PAUSED = os.environ.get("SEATGEEK_PAUSED", "0") == "1"


def main():
    if PAUSED:
        print("[watch] PAUSED — not scraping (flip PAUSED / SEATGEEK_PAUSED=0 to resume)")
        return
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

    # Pair each event with its mirror on the OTHER ticket site (same show, other
    # platform) so flips get sanity-checked against the real cross-market price.
    # Pairing is by the row label with the platform tag stripped: "Cubs 6/20" and
    # "Cubs 6/20 SH" -> same base -> mirrors. No label or no mirror -> no check.
    def _platform(u):
        u = (u or "").lower()
        return "sh" if "stubhub.com" in u else ("sg" if "seatgeek.com" in u else "?")
    def _base_label(r):
        b = re.sub(r"\b(sh|stubhub|sg|seatgeek)\b", "", (r.get("label") or "").lower())
        return re.sub(r"\s+", " ", b).strip()
    by_base = defaultdict(dict)  # base label -> {platform: url}
    for r in wl:
        b = _base_label(r)
        if b:
            by_base[b][_platform(r["url"])] = r["url"]
    def _mirror_url(r):
        plat = _platform(r["url"])
        other = "sg" if plat == "sh" else "sh"
        return by_base.get(_base_label(r), {}).get(other)

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
        # mirror event on the other platform (cached; scraped at most once/run)
        murl = _mirror_url(row)
        mlist = None
        if murl:
            if murl not in scrape_cache:
                scrape_cache[murl] = get_listings(murl)
            mlist = scrape_cache[murl] or None
        matches, limit, candidates = evaluate(row, listings, mirror_listings=mlist)
        # seen = {deal signature -> last-alerted epoch}. Cooldown-based so a deal that
        # flickers in and out of the scrape (or gets re-keyed by the platform) can't
        # re-spam. Migrate off the old list-of-ids schema on first run.
        seen = est.get("seen")
        if not isinstance(seen, dict):
            seen = {}
            est["seen"] = seen
        cooldown = ALERT_COOLDOWN_HRS * 3600
        ignored = set(state.get("ignored", []))
        eid = event_id_from_url(url)
        cheapest = min(candidates, key=lambda L: L["price"]) if candidates else None
        if cheapest:
            lim = f"  (buy-line ${limit:.0f})" if limit else ""
            print(f"   cheapest watched: {cheapest['section']} ${cheapest['price']:.0f}{lim} · {len(matches)} deal(s)")
        sent = 0
        for L in matches:
            sig = deal_sig(eid, L)
            # Per-section key (event + normalized section, ignoring row/price) so we
            # can rate-limit a whole section, not just one exact deal signature.
            sect_key = "SECT|" + str(eid) + "|" + re.sub(r"\s+", " ", str(L.get("section") or "")).strip().lower()
            if mute_key(eid, L["section"]) in ignored:
                seen[sig] = now  # muted section -> record as seen, never alert
                continue
            if now - seen.get(sig, 0) < cooldown:
                continue  # alerted this same deal recently -> stay quiet
            # Per-SECTION cooldown: a hot section's cheapest seat churns (one sells,
            # the next relists a few dollars off), making a fresh deal_sig each time
            # and spamming the same section 3x a day. Cap it: once a section alerts,
            # stay quiet on it for the whole cooldown window regardless of price wiggle.
            if now - seen.get(sect_key, 0) < cooldown:
                seen[sig] = now
                continue
            if sent < MAX_ALERTS:
                print(f"   ALERT {L['section']} ${L['price']:.0f}")
                send_alert(label, row["url"], L, limit)
                sent += 1
                seen[sect_key] = now  # start this section's cooldown (only on a real send)
            # mark seen even when not sent, so a big match-set never floods:
            # you get the cheapest MAX, the rest are recorded silently.
            seen[sig] = now
        if sent:
            print(f"   sent {sent} alert(s) (cheapest first, capped at {MAX_ALERTS})")
        # bound state: drop signatures untouched for a week (past any cooldown)
        cutoff = now - max(cooldown, 7 * 86400)
        est["seen"] = {k: v for k, v in seen.items() if v >= cutoff}

    save_state(state)
    print("[watch] done")


if __name__ == "__main__":
    main()
