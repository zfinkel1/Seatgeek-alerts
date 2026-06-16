"""
SeatGeek section-level listings via Scrapfly (direct API).

The old approach drove a Bright Data remote browser to load the full event page
and capture the event_listings_v2 XHR — SeatGeek's PerimeterX/DataDome flagged
that fingerprint under heavy use. New approach: hit the event_listings_v2 JSON
API DIRECTLY through Scrapfly's Anti-Scraping-Protection (asp=true). No full page
render — much cheaper + faster, and Scrapfly fights the anti-bot for us.

client_id is SeatGeek's stable public web client id (decodes to "1662|..."),
not a session token, so it's reusable. Override via SEATGEEK_CLIENT_ID if it ever
rotates (re-grab it from any event page's `?client_id=` XHR param).
"""
import os
import re
import json
import urllib.parse
import urllib.request

SEATGEEK_CLIENT_ID = os.environ.get("SEATGEEK_CLIENT_ID", "MTY2MnwxMzgzMzIwMTU4")


def _event_id(url):
    m = re.search(r"/(\d{6,})(?:[/?#]|$)", url) or re.search(r"(\d{6,})", url)
    return m.group(1) if m else None


def _find_listings(o):
    """Recursively find the array of listing dicts (has section `s` + a price)."""
    if isinstance(o, list) and o and isinstance(o[0], dict):
        k = o[0]
        if "s" in k and any(p in k for p in ("dp", "pf", "p", "price")):
            return o
    if isinstance(o, dict):
        for v in o.values():
            r = _find_listings(v)
            if r:
                return r
    elif isinstance(o, list):
        for v in o:
            r = _find_listings(v)
            if r:
                return r
    return None


def get_listings(event_url, retries=2):
    """
    Return normalized listings for a SeatGeek event:
        {"section","price"(all-in),"qty","row","id","value","score"}
    Empty list if it couldn't pull them (caller decides whether to retry later).
    """
    key = os.environ.get("SCRAPFLY_KEY")
    if not key:
        print("  [scrape] SCRAPFLY_KEY not set")
        return []
    eid = _event_id(event_url)
    if not eid:
        print(f"  [scrape] could not parse event id from {event_url}")
        return []

    target = (f"https://seatgeek.com/api/event_listings_v2"
              f"?id={eid}&client_id={SEATGEEK_CLIENT_ID}")
    api = ("https://api.scrapfly.io/scrape?key=" + urllib.parse.quote(key)
           + "&url=" + urllib.parse.quote(target, safe="")
           + "&asp=true&country=us")

    last_err = None
    for _ in range(1, retries + 1):
        try:
            # Scrapfly's anti-bot (asp) calls can run 30-120s; give them headroom.
            with urllib.request.urlopen(api, timeout=150) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            result = payload.get("result", {})
            if result.get("status_code") != 200:
                last_err = f"target status {result.get('status_code')}"
                continue
            data = json.loads(result.get("content") or "{}")
            arr = _find_listings(data) or []
            out = []
            for L in arr:
                price = L.get("dp") or L.get("pf") or L.get("p")
                if price is None:
                    continue
                dq = L.get("dq") or {}
                score = dq.get("ddq")
                try:
                    score = int(float(score)) if score is not None else None
                except (TypeError, ValueError):
                    score = None
                out.append({
                    "section": str(L.get("s") or "").strip(),
                    "price": float(price),
                    "qty": L.get("q"),
                    "row": L.get("r"),
                    "id": str(L.get("id") or f"{L.get('s')}-{price}"),
                    "value": float(dq["ev"]) if dq.get("ev") else None,
                    "score": score,
                })
            if out:
                return out
            last_err = "no listings in API response"
        except Exception as e:
            last_err = e
    if last_err:
        print(f"  [scrape] failed after {retries} tries: {last_err}")
    return []
