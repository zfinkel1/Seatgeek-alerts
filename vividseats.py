"""
Vivid Seats section-level listings via its internal 'Hermes' JSON API.

Endpoint: vividseats.com/hermes/api/v1/listings?productionId=<id>
Response { "global":[...meta...], "tickets":[...listings...] }.
`allInPricePerTicket` (aip) is the true all-in buyer cost; `p` is pre-fee.

Akamai Bot Manager guards the edge and challenges plain datacenter requests
(returns an HTML "Challenge Validation" page, not JSON). So we fetch through
Scrapfly ASP (residential) when SCRAPFLY_KEY is set — proven to return the full
JSON — and only fall back to a direct GET when there's no key (e.g. a laptop on
a residential IP). Section names carry a tier prefix ("Club Level 202",
"Grandstand 445"); the number still substring-matches a section watch, and buy
rows bypass the club/suite exclusions so those premium tiers still alert.

Event/production id = the number after /production/ in the URL (the slug before
it is cosmetic and can be wrong — only /production/{id} matters).
"""
import os
import re
import json
import urllib.parse
import urllib.request
import urllib.error

API = "https://www.vividseats.com/hermes/api/v1/listings"
SCRAPFLY = "https://api.scrapfly.io/scrape"
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")


def _production_id(url):
    m = re.search(r"/production/(\d+)", url or "")
    if m:
        return m.group(1)
    m = re.search(r"productionId=(\d+)", url or "")
    return m.group(1) if m else None


def _int(v):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _scrapfly_get(key, target, timeout=150):
    params = {
        "key": key,
        "url": target,
        "asp": "true",
        "country": "us",
        # Residential IPs clear Akamai far more reliably than datacenter ones.
        # Overridable (SCRAPFLY_PROXY_POOL); set empty to drop the param.
        "proxy_pool": os.environ.get("SCRAPFLY_PROXY_POOL", "public_residential_pool"),
    }
    if not params["proxy_pool"]:
        del params["proxy_pool"]
    u = SCRAPFLY + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(u, timeout=timeout) as r:
        o = json.loads(r.read().decode("utf-8", "replace"))
    res = o.get("result", {})
    if res.get("status_code") != 200:
        raise RuntimeError(f"vivid upstream status {res.get('status_code')}")
    return json.loads(res.get("content") or "{}")


def _direct_get(target, timeout=40):
    req = urllib.request.Request(target, headers={
        "User-Agent": _UA,
        "Accept": "application/json",
        "Referer": "https://www.vividseats.com/",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _fetch(target):
    key = os.environ.get("SCRAPFLY_KEY")
    if key:
        return _scrapfly_get(key, target)
    return _direct_get(target)


def get_listings(event_url, retries=3):
    """Normalized listings {section, price(all-in), qty, row, id, value, score, flags}."""
    pid = _production_id(event_url)
    if not pid:
        print(f"  [vivid] could not parse production id from {event_url}")
        return []
    api_url = f"{API}?productionId={pid}&currency=USD"
    last_err = None
    for _ in range(retries):
        try:
            data = _fetch(api_url)
            arr = data.get("tickets") or []
            out = []
            for L in arr:
                price = L.get("allInPricePerTicket") or L.get("aip") or L.get("p")
                if price is None:
                    continue
                out.append({
                    "section": str(L.get("sectionName") or L.get("s") or "").strip(),
                    "price": float(price),
                    "qty": _int(L.get("quantity") or L.get("q")),
                    "row": L.get("row") or L.get("r"),
                    "id": str(L.get("i") or L.get("id") or f"{L.get('sectionName')}-{price}"),
                    "value": None,
                    "score": None,
                    "flags": "",
                })
            if out:
                return out
            last_err = "no tickets in response"
        except Exception as e:
            last_err = e
    if last_err:
        print(f"  [vivid] failed after {retries} tries: {last_err}")
    return []
