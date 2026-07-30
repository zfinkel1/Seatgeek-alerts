"""
TicketGenie API client.

Two jobs:

1. A SIXTH SOURCE for the watcher. `get_listings(url)` returns the exact same
   normalized shape the five scrapers return — {section, price, qty, row, id,
   value, score, flags} — so it drops into watch.py's router with no downstream
   changes. That matters because the current scrapers fight anti-bot systems for
   a living: scrape.py notes SeatGeek hardened PerimeterX in July 2026 and forced
   us onto residential proxies. An API call has no such failure mode.

2. RESEARCH ACCESS for signals.py — confirmed sales, historical listing snapshots,
   and section capacities.

BILLING SHAPE — this drives every design choice here.
Charges are per successful CALL by pricing class, NOT per row returned. So:
  - always request the maximum page size (a 2000-row page costs the same as a
    10-row page),
  - the bulk snapshot endpoint saves round-trips but NOT money ($0.03 per
    snapshot resolved either way),
  - and every paginated helper below takes a hard `max_pages` cap so a runaway
    loop can't quietly drain the wallet.

Auth: Authorization: ApiKey tg_ak_...   (env TICKETGENIE_API_KEY)
"""
import os
import re
import json
import urllib.error
import urllib.request

BASE = os.environ.get("TICKETGENIE_BASE", "https://api.ticketgenie.io/api/v1")

# Page ceilings straight from the spec. Anything smaller wastes money, since the
# call is billed the same regardless of how many rows come back.
MAX_PAGE = {
    "sales": 1000,
    "snapshots": 1000,
    "snapshot_details": 2000,
    "listings": 2000,
}

# Watchlist URLs -> TicketGenie {id, platform}. The numeric id in each site's URL
# is that platform's native event id, which is exactly what the API keys on.
_PLATFORM_HOSTS = (
    ("stubhub.com", "stubhub"),
    ("seatgeek.com", "seatgeek"),
    ("gametime.co", "gametime"),
    ("vividseats.com", "vividseats"),
    ("ticketmaster.com", "ticketmaster"),
    ("axs.com", "axs"),
)


class TicketGenieError(RuntimeError):
    """Non-2xx from the API. `status` lets callers treat 403 (a permission the key
    lacks) differently from 500 (retry) without parsing message text."""

    def __init__(self, status, payload):
        self.status = status
        self.payload = payload
        super().__init__(f"HTTP {status}: {json.dumps(payload)[:300]}")


def native_from_url(url):
    """{'id','platform'} from a watchlist URL, or None if it can't be read."""
    u = (url or "").lower()
    platform = next((name for host, name in _PLATFORM_HOSTS if host in u), None)
    if not platform:
        return None
    m = re.search(r"/event/(\d+)", u) or re.search(r"(\d{6,})", u)
    return {"id": m.group(1), "platform": platform} if m else None


def _transport(method, url, body, headers, timeout):
    """Isolated so tests can swap the network out. Returns (status, parsed)."""
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        try:
            payload = json.loads(e.read().decode("utf-8", "replace"))
        except Exception:
            payload = {"message": str(e.reason)}
        return e.code, payload


class TicketGenie:
    def __init__(self, api_key=None, base=BASE, transport=None):
        self.api_key = api_key or os.environ.get("TICKETGENIE_API_KEY")
        self.base = base
        self._transport = transport or _transport
        self.calls = 0          # every billable call, so cost is auditable

    def _call(self, method, path, body=None, timeout=90):
        if not self.api_key:
            raise TicketGenieError(0, {"message": "TICKETGENIE_API_KEY not set"})
        self.calls += 1
        status, payload = self._transport(
            method, self.base + path, body,
            {"Authorization": f"ApiKey {self.api_key}",
             "Content-Type": "application/json"},
            timeout,
        )
        if status < 200 or status >= 300:
            raise TicketGenieError(status, payload)
        return payload

    def _paginate(self, path, body, items_key, limit, max_pages=20):
        """Keyset pagination. The API returns page.nextCursor {sortValue, id};
        we echo it back as page.cursor until it stops coming.

        `max_pages` is a spend guard, not a correctness knob — when it trips we
        say so out loud rather than silently returning a truncated result, since
        a quietly short answer reads as 'that's all the data' when it isn't."""
        body = dict(body)
        body.setdefault("page", {})["limit"] = limit
        out, pages = [], 0
        while True:
            payload = self._call("POST", path, body)
            rows = payload.get(items_key) or []
            out.extend(rows)
            pages += 1
            cursor = (payload.get("page") or {}).get("nextCursor")
            if not cursor or not rows:
                return out, payload
            if pages >= max_pages:
                print(f"  [tg] {path}: stopped at max_pages={max_pages} "
                      f"({len(out)} rows so far, MORE AVAILABLE)")
                return out, payload
            body["page"] = {"limit": limit, "cursor": cursor}

    # ------------------------------------------------------------ live listings

    @staticmethod
    def normalize(rows):
        """API listing rows -> the scrapers' shape, so the flip engine is source-
        agnostic. `totalPrice` (fee-inclusive) is preferred over the advertised
        `price` when present: watch.py compares against all-in buy prices, and
        mixing advertised and all-in numbers silently corrupts every margin."""
        out = []
        for L in rows:
            price = L.get("totalPrice") or L.get("price")
            if price is None:
                continue
            out.append({
                "section": str(L.get("section") or "").strip(),
                "price": float(price),
                "qty": L.get("quantity"),
                "row": L.get("row"),
                "id": str(L.get("id") or f"{L.get('section')}-{price}"),
                "value": None,          # no per-listing fair-value estimate here
                "score": None,
                "flags": "accessible" if L.get("accessible") else "",
                "primary": bool(L.get("primary")),
            })
        return out

    def current_listings(self, native, inventory_type=None, max_pages=10):
        body = {"nativeId": native}
        if inventory_type:
            body["inventoryType"] = inventory_type
        rows, _ = self._paginate("/events/listings/current", body, "listings",
                                 MAX_PAGE["listings"], max_pages)
        return self.normalize(rows)

    def get_listings(self, url):
        """Drop-in for watch.py's router. Returns [] rather than raising, matching
        the scrapers' contract — a dead source must not kill the whole run."""
        native = native_from_url(url)
        if not native:
            print(f"  [tg] could not parse a native id from {url}")
            return []
        try:
            return self.current_listings(native)
        except TicketGenieError as e:
            print(f"  [tg] {e}")
            return []

    # ------------------------------------------------------------- research

    def sales(self, native, start, end, max_pages=20):
        """Confirmed sales: {date, atp, section, row, place, quantity}. The only
        true demand observation available — there is no bid side to read."""
        rows, payload = self._paginate(
            "/events/sales/timeseries",
            {"nativeId": native, "dateRange": {"from": start, "to": end},
             "sort": {"field": "DATE", "order": "ASC"}},
            "sales", MAX_PAGE["sales"], max_pages)
        return rows, payload.get("availableDateRange")

    def snapshots(self, native, start, end, interval="1h",
                  inventory_type=None, max_pages=10):
        """Available snapshot timestamps + listing counts. Cheap reconnaissance —
        call this before pulling details, because details bill $0.03 EACH."""
        body = {"nativeId": native, "dateRange": {"from": start, "to": end},
                "interval": interval,
                "sort": {"field": "TIMESTAMP", "order": "ASC"}}
        if inventory_type:
            body["inventoryType"] = inventory_type
        rows, payload = self._paginate(body=body,
                                       path="/events/listings/timeseries/snapshots",
                                       items_key="snapshots",
                                       limit=MAX_PAGE["snapshots"],
                                       max_pages=max_pages)
        return rows, payload.get("availableDateRange")

    def snapshot_listings(self, native, timestamp, interval="1h",
                          inventory_type=None, max_pages=5):
        """The full ask ladder at one point in time. $0.03 per snapshot."""
        body = {"nativeId": native, "timestamp": timestamp, "interval": interval}
        if inventory_type:
            body["inventoryType"] = inventory_type
        rows, _ = self._paginate("/events/listings/timeseries/snapshots/details",
                                 body, "listings", MAX_PAGE["snapshot_details"],
                                 max_pages)
        return self.normalize(rows)

    def section_capacities(self, native):
        """Ticketmaster section + subsection capacities: the denominator watch.py
        has never had. Percent-of-capacity-sold beats raw listing counts, and it
        is what makes zones learnable instead of guessed from section numbers."""
        return self._call(
            "GET", f"/events/{native['platform']}/{native['id']}/seatmap/sections")

    # -------------------------------------------------------------- screening

    def screen(self, *, inventory=None, price=None, sales_filter=None,
               details=None, sort=None, limit=200):
        """Market-wide scan with SERVER-SIDE filters — the capability the current
        218-row watchlist fundamentally lacks.

        Filtering here rather than locally is the whole game economically: pulling
        150k events to filter them on our side would cost orders of magnitude more
        than asking the API for the handful that already match.

        inventory/price: [{platform, inventoryType, op, value}]
        sales_filter:    {interval, quantity|volume|average: [{op, value}]}
        """
        body = {"meta": {"limit": limit}}
        if details:
            body["byDetails"] = details
        if inventory or price:
            inv = {}
            if inventory:
                inv["inventory"] = inventory
            if price:
                inv["price"] = price
            body["byInventory"] = inv
        if sales_filter:
            body["bySale"] = sales_filter
        if sort:
            body["sort"] = sort
        payload = self._call("POST", "/events", body)
        return payload.get("events") or payload.get("data") or [], payload

    def usage_pricing(self):
        return self._call("GET", "/usage-pricing")


# ------------------------------------------------------------------ self-test

def _selftest():
    """Exercises pagination, normalization, and error handling against a fake
    transport — so the client is known-correct before a paid call is ever made."""
    print("ticketgenie.py self-test\n" + "-" * 60)
    seen = []

    def fake(method, url, body, headers, timeout):
        seen.append((method, url, body))
        if url.endswith("/events/listings/current"):
            cursor = (body.get("page") or {}).get("cursor")
            if not cursor:
                return 200, {"listings": [
                    {"id": "L1", "section": "117", "price": 88.0,
                     "totalPrice": 101.2, "quantity": 2, "row": "12"},
                    {"id": "L2", "section": "118", "price": 95.0,
                     "quantity": 4, "row": "3", "accessible": True},
                ], "page": {"nextCursor": {"sortValue": "95.0", "id": "L2"}}}
            return 200, {"listings": [
                {"id": "L3", "section": "119", "price": 120.0,
                 "quantity": 1, "row": "9", "primary": True},
            ], "page": {}}
        if url.endswith("/events/sales/timeseries"):
            return 200, {"sales": [{"date": "2026-07-01", "atp": 110.0,
                                    "section": "117", "quantity": 2}],
                         "availableDateRange": {"start": "2026-05-01",
                                                "end": "2026-07-30"},
                         "page": {}}
        if url.endswith("/seatmap/sections"):
            return 403, {"code": "INSUFFICIENT_PERMISSIONS"}
        return 200, {}

    tg = TicketGenie(api_key="tg_ak_test", transport=fake)

    native = native_from_url("https://seatgeek.com/x/concert/18076657")
    print("native_from_url:", native)
    assert native == {"id": "18076657", "platform": "seatgeek"}
    sh = native_from_url("https://www.stubhub.com/event/160434798")
    assert sh == {"id": "160434798", "platform": "stubhub"}, sh
    assert native_from_url("https://example.com/nope") is None

    listings = tg.current_listings(native)
    print(f"listings across 2 pages: {len(listings)}")
    for L in listings:
        print("  ", L)
    assert len(listings) == 3, "pagination must follow nextCursor"
    assert listings[0]["price"] == 101.2, "totalPrice must win over price"
    assert listings[1]["price"] == 95.0, "falls back to price when no totalPrice"
    assert listings[1]["flags"] == "accessible"
    assert listings[2]["primary"] is True
    assert set(listings[0]) >= {"section", "price", "qty", "row", "id",
                                "value", "score", "flags"}, "scraper shape"

    sales, rng = tg.sales(native, "2026-05-01T00:00:00Z", "2026-07-30T00:00:00Z")
    print(f"sales={len(sales)} availableDateRange={rng}")
    assert rng["start"] == "2026-05-01"

    try:
        tg.section_capacities(native)
        raise AssertionError("403 should have raised")
    except TicketGenieError as e:
        print(f"403 surfaced as: {e.status} {e.payload['code']}")
        assert e.status == 403

    # Valid, parseable URL — so this exercises the MISSING-KEY path rather than
    # bailing earlier on an unreadable id.
    unset = TicketGenie(api_key=None, transport=fake)
    assert unset.get_listings("https://seatgeek.com/x/concert/18076657") == [], \
        "missing key must degrade to [] like the scrapers, not crash the run"
    assert unset.calls == 0, "must not count a call it never made"

    # Max page sizes must actually be requested — the call is billed the same
    # whether it returns 10 rows or 2000.
    limits = [b.get("page", {}).get("limit") for _, u, b in seen
              if b and u.endswith("/events/listings/current")]
    print("requested page limits:", limits)
    assert all(l == MAX_PAGE["listings"] for l in limits), limits

    print(f"billable calls counted: {tg.calls}")
    assert tg.calls == 4, tg.calls
    print("-" * 60)
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
