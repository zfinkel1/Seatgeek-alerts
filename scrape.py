"""
SeatGeek section-level listing fetcher via Bright Data Browser API.

A real remote browser (Bright Data) loads the event page — beating PerimeterX —
and we capture the `event_listings_v2` XHR it fires, which has every listing with
section + price. Images/fonts/css are blocked to keep bandwidth (cost) down.
"""
import os
from playwright.sync_api import sync_playwright

_BLOCK = {"image", "media", "font", "stylesheet"}


def _find_listings(o):
    """Recursively find the array of listing dicts (has section `s` + a price field)."""
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
    Return a list of normalized listings for a SeatGeek event:
        {"section": str, "price": float (all-in), "qty": int|None, "row": str|None, "id": str}
    Empty list if it couldn't pull them (caller decides whether to retry later).
    """
    wss = os.environ.get("BRIGHTDATA_BROWSER_WSS")
    if not wss:
        print("  [scrape] BRIGHTDATA_BROWSER_WSS not set")
        return []
    last_err = None
    for attempt in range(1, retries + 1):
        try:
            with sync_playwright() as pw:
                browser = pw.chromium.connect_over_cdp(wss, timeout=60000)
                try:
                    page = browser.new_page()
                    # Collect the page's OWN listings XHR (it's authorized — carries the
                    # client_id + cookies). No asset-blocking: tampering trips PerimeterX.
                    hits = []
                    page.on("response", lambda r: hits.append(r) if "event_listings" in r.url else None)
                    page.goto(event_url, wait_until="domcontentloaded", timeout=90000)
                    raw = None
                    for _ in range(40):  # wait up to ~40s for the listings call
                        for r in list(hits):
                            try:
                                if r.status == 200:
                                    raw = r.json()
                                    break
                            except Exception:
                                pass
                        if raw is not None:
                            break
                        page.wait_for_timeout(1000)
                    if raw is None:
                        last_err = "no listings response captured"
                        continue  # retry
                    arr = _find_listings(raw) or []
                    out = []
                    for L in arr:
                        price = L.get("dp") or L.get("pf") or L.get("p")
                        if price is None:
                            continue
                        out.append({
                            "section": str(L.get("s") or "").strip(),
                            "price": float(price),
                            "qty": L.get("q"),
                            "row": L.get("r"),
                            "id": str(L.get("id") or f"{L.get('s')}-{price}"),
                        })
                    return out
                finally:
                    browser.close()
        except Exception as e:
            last_err = e
    if last_err:
        print(f"  [scrape] failed after {retries} tries: {last_err}")
    return []
