"""
SeatGeek section-level listings via Scrapfly (session-based).

SeatGeek guards the event_listings_v2 JSON API hard — a bare request through
Scrapfly's ASP only gets through ~28% of the time (it lacks the session cookies
the page establishes). The fix: Scrapfly SESSIONS.
  1. Render the full event page (render_js) under a session name — this passes
     the anti-bot reliably and populates the session with valid cookies.
  2. Call the event_listings_v2 API under the SAME session — it carries those
     cookies and goes through cleanly.

client_id is SeatGeek's stable public web client id; override via
SEATGEEK_CLIENT_ID if it ever rotates.
"""
import os
import re
import json
import urllib.parse
import urllib.request

SEATGEEK_CLIENT_ID = os.environ.get("SEATGEEK_CLIENT_ID", "MTY2MnwxMzgzMzIwMTU4")
SCRAPFLY = "https://api.scrapfly.io/scrape"


def _event_id(url):
    m = re.search(r"/(\d{6,})(?:[/?#]|$)", url) or re.search(r"(\d{6,})", url)
    return m.group(1) if m else None


def _scrapfly_url(key, target, session, render_js=False):
    params = {
        "key": key,
        "url": target,
        "asp": "true",
        "country": "us",
        "session": session,
        "session_sticky_proxy": "true",
    }
    if render_js:
        params["render_js"] = "true"
    return SCRAPFLY + "?" + urllib.parse.urlencode(params)


def _call(url, timeout=150):
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


def _find_listings(o):
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


def get_listings(event_url, retries=3):
    """
    Return normalized listings for a SeatGeek event:
        {"section","price"(all-in),"qty","row","id","value","score"}
    Empty list if it couldn't pull them after `retries` attempts.
    """
    key = os.environ.get("SCRAPFLY_KEY")
    if not key:
        print("  [scrape] SCRAPFLY_KEY not set")
        return []
    eid = _event_id(event_url)
    if not eid:
        print(f"  [scrape] could not parse event id from {event_url}")
        return []

    session = "sg" + eid
    api_target = (f"https://seatgeek.com/api/event_listings_v2"
                  f"?id={eid}&client_id={SEATGEEK_CLIENT_ID}")
    page_url = _scrapfly_url(key, event_url, session, render_js=True)
    api_url = _scrapfly_url(key, api_target, session, render_js=False)

    last_err = None
    for _ in range(retries):
        try:
            # 1. render the event page to establish/refresh the session cookies
            _call(page_url)
            # 2. pull the listings API within that same session
            payload = _call(api_url)
            result = payload.get("result", {})
            if result.get("status_code") != 200:
                last_err = f"api target status {result.get('status_code')}"
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
