"""
Ticket Exchange by Ticketmaster (ticketexchangebyticketmaster.com) resale
listings via Scrapfly.

This is Ticketmaster's legacy TicketExchange resale site, protected by Imperva
reese84 + PerimeterX. Scrapfly asp=true + render_js clears the anti-bot and
returns the fully-rendered page with the resale inventory baked into the DOM
(verified live: 108 Lollapalooza listings). Each listing is a repeater item:
  <p data-locator="section-<id>">TYPE</p>  ticket type — for a festival this is
       the DAY for general admission ("Thursday" = GA) plus "VIPThu"/"PlatThu"/
       "GA+Thu" for the premium tiers.
  data-inv-price="<base>"  data-serv-chrg="<fee>"   all-in = base + fee

Event id = the trailing number in /tickets/<id>. Price returned is ALL-IN (what
the buyer actually pays), matching the big price the page shows.
"""
import os
import re
import json
import urllib.parse
import urllib.request

SCRAPFLY = "https://api.scrapfly.io/scrape"


def _event_id(url):
    m = re.search(r'/tickets/(\d+)', url or '') or re.search(r'(\d{6,})', url or '')
    return m.group(1) if m else None


def _scrapfly_url(key, target, wait=8000):
    params = {
        "key": key, "url": target, "asp": "true", "country": "us",
        "render_js": "true", "rendering_wait": str(wait),
        # Residential clears Ticketmaster's stack far more reliably than datacenter.
        "proxy_pool": os.environ.get("SCRAPFLY_PROXY_POOL", "public_residential_pool"),
    }
    if not params["proxy_pool"]:
        del params["proxy_pool"]
    return SCRAPFLY + "?" + urllib.parse.urlencode(params)


def parse_listings(html):
    """Each resale row is a `tmr-repeater-item`; pull its ticket-type name and
    base price + service charge (all-in = the two summed)."""
    out = []
    chunks = re.split(r'data-locator="tmr-repeater-item-\d+"', html)
    for it in chunks[1:]:
        ms = re.search(r'data-locator="section-(\d+)">([^<]+)', it)
        mp = re.search(r'data-inv-price="(\d+)"', it)
        if not (ms and mp):
            continue
        mf = re.search(r'data-serv-chrg="([\d.]+)"', it)
        base = float(mp.group(1))
        fee = float(mf.group(1)) if mf else 0.0
        out.append({
            "section": ms.group(2).strip(),
            "price": round(base + fee, 2),   # all-in
            "qty": None,
            "row": None,
            "id": ms.group(1),
            "value": None,
            "score": None,
            "flags": "",
        })
    return out


def get_listings(event_url, retries=3):
    """Normalized listings {section, price(all-in), qty, row, id, value, score, flags}."""
    key = os.environ.get("SCRAPFLY_KEY")
    if not key:
        print("  [ticketexchange] SCRAPFLY_KEY not set")
        return []
    url = _scrapfly_url(key, event_url)
    last_err = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=200) as resp:
                payload = json.loads(resp.read().decode("utf-8", "replace"))
            result = payload.get("result", {})
            if result.get("status_code") != 200:
                last_err = f"status {result.get('status_code')}"
                continue
            out = parse_listings(result.get("content") or "")
            if out:
                return out
            last_err = "no listings parsed"
        except Exception as e:
            last_err = e
    if last_err:
        print(f"  [ticketexchange] failed after {retries} tries: {last_err}")
    return []
