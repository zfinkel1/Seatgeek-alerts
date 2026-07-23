"""
Gametime section-level listings via its public mobile JSON API.

No auth, no anti-bot: one GET to mobile.gametime.co/v1/listings?event_id=<id>
returns the whole event's inventory. Prices are integer CENTS; price.total is
the all-in buyer price. `section` is the real section ("225"); `section_group`
is a human label ("Mid Level Endzone") — we match/alert on `section` so the
number-based section watches line up with the other platforms.

Event id = the trailing 24-hex Mongo ObjectId in the event URL
(/events/695c58588ac525266dacf81e).
"""
import re
import json
import urllib.parse
import urllib.request

API = "https://mobile.gametime.co/v1/listings"
_UA = ("Gametime/2 CFNetwork/1490.0.4 Darwin/23.6.0")


def _event_id(url):
    m = re.search(r"/events/([0-9a-f]{24})", url or "", re.I)
    if m:
        return m.group(1)
    m = re.search(r"\b([0-9a-f]{24})\b", url or "", re.I)
    return m.group(1) if m else None


def _get(url, timeout=40):
    req = urllib.request.Request(url, headers={
        "User-Agent": _UA,
        "Accept": "application/json",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def get_listings(event_url, retries=3):
    """Normalized listings {section, price(all-in), qty, row, id, value, score, flags}."""
    eid = _event_id(event_url)
    if not eid:
        print(f"  [gametime] could not parse event id from {event_url}")
        return []
    api_url = f"{API}?event_id={urllib.parse.quote(eid)}"
    last_err = None
    for _ in range(retries):
        try:
            data = _get(api_url)
            arr = data.get("listings") or []
            out = []
            for L in arr:
                price = (L.get("price") or {}).get("total")
                if price is None:
                    continue
                lots = L.get("lots") or []
                qty = lots[0] if lots else None
                out.append({
                    "section": str(L.get("section") or "").strip(),
                    "price": float(price) / 100.0,   # cents -> dollars
                    "qty": qty,
                    "row": L.get("row"),
                    "id": str(L.get("id") or f"{L.get('section')}-{price}"),
                    "value": None,
                    "score": None,
                    "flags": "",
                })
            if out:
                return out
            last_err = "no listings in response"
        except Exception as e:
            last_err = e
    if last_err:
        print(f"  [gametime] failed after {retries} tries: {last_err}")
    return []
