"""
StubHub section-level listings via Scrapfly.

StubHub embeds the FULL inventory in the rendered page (no separate listings API
to chase — the only XHRs are anti-bot challenges). With Scrapfly asp + render_js
+ a render wait, the page bakes in two maps, both keyed by "{ticketClassId}_{sectionId}":
  - venueMapData.sectionPopupData[key] -> { rawMinPrice, ticketCount, rowText,
      rawMinPriceDealScore, ... }   (cheapest offering per section/row group)
  - venueConfiguration[key]          -> { sectionId, sectionName, ... }

We render once, extract both maps, and join them into the same normalized shape
the SeatGeek scraper returns: {section, price, qty, row, id, value, score}.

NOTE: rawMinPrice is StubHub's pre-fee price; the flip margins give buffer, and
comparison is StubHub-vs-StubHub (within an event) so it's apples-to-apples.
"""
import os
import re
import json
import urllib.parse
import urllib.request

SCRAPFLY = "https://api.scrapfly.io/scrape"


def _event_id(url):
    m = re.search(r"/event/(\d+)", url) or re.search(r"(\d{6,})", url)
    return m.group(1) if m else None


def _extract_json_object(text, key):
    """Return the balanced-brace JSON object that follows "key": in text."""
    m = re.search(r'"' + re.escape(key) + r'"\s*:\s*\{', text)
    if not m:
        return None
    start = m.end() - 1  # index of the opening '{'
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
        elif ch == "\\":
            esc = True
        elif ch == '"':
            in_str = not in_str
        elif not in_str:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        return json.loads(text[start:i + 1])
                    except Exception:
                        return None
    return None


def parse_listings(content):
    """Join sectionPopupData + venueConfiguration into normalized listings."""
    popup = _extract_json_object(content, "sectionPopupData")
    config = _extract_json_object(content, "venueConfiguration")
    if not popup:
        return []
    config = config or {}
    out = []
    for key, p in popup.items():
        if not isinstance(p, dict):
            continue
        price = p.get("rawMinPrice")
        if price is None:
            continue
        cfg = config.get(key) or {}
        section = (cfg.get("sectionName") or p.get("sectionName") or "").strip()
        score = p.get("rawMinPriceDealScore")
        try:
            score = int(float(score)) if score is not None else None
        except (TypeError, ValueError):
            score = None
        out.append({
            "section": section or str(cfg.get("sectionId") or ""),
            "price": float(price),
            "qty": p.get("ticketCount") or p.get("count"),
            "row": p.get("rowText"),
            "id": str(key),
            "value": None,
            "score": score,
        })
    return out


def _scrapfly_url(key, target, render_js=True, wait=6000):
    params = {
        "key": key, "url": target, "asp": "true", "country": "us",
    }
    if render_js:
        params["render_js"] = "true"
        params["rendering_wait"] = str(wait)
    return SCRAPFLY + "?" + urllib.parse.urlencode(params)


def get_listings(event_url, retries=3):
    """Normalized StubHub listings, or [] after `retries` failed attempts."""
    key = os.environ.get("SCRAPFLY_KEY")
    if not key:
        print("  [stubhub] SCRAPFLY_KEY not set")
        return []
    url = _scrapfly_url(key, event_url, render_js=True)
    last_err = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(url, timeout=180) as resp:
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
        print(f"  [stubhub] failed after {retries} tries: {last_err}")
    return []
