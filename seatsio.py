"""
seats.io reader — seat-level primary inventory for any venue on the platform.

WHY THIS AND NOT A SCRAPER PER SITE
seats.io is the widely-used independent reserved-seating platform. Every venue on
it embeds the same three values in its checkout page -- workspaceKey, event key,
and the price table -- because the browser needs them to draw the chart. So one
reader works across every seats.io venue instead of one scraper per site. That is
the difference between this being a How To Academy tool and a platform tool.

It also doubles as the qualifier we actually want: a venue running seats.io has
RESERVED SEATING, which is the precondition for a resale market. General-admission
club nights measured 0-3 upcoming SeatGeek events -- effectively no secondary.

WHAT IT PRODUCES
A work order: section, row, consecutive seat numbers, price, quantity. The output
has to be executable by a buyer with no market context and no judgement calls.

WHAT IT DOES NOT TELL YOU
Whether the event is worth buying. `forSale` objects are the seats the operator
has released for sale; combine with secondary comps (and mirror detection) before
acting. Inventory is not opportunity.
"""
import re
import json
import urllib.request
from collections import defaultdict

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
RENDER_HOSTS = ("https://cdn.seatsio.net", "https://cdn-eu.seatsio.net")

# Bands that are cheap for a reason and resell badly. Picking on price alone
# lands on these every time -- the first run of this reader proposed "Band F
# (Very Restricted View)" purely because it was the cheapest. watch.py has
# excluded the same categories from the flip engine for the same reason.
BAD_VIEW = re.compile(
    r"restricted|obstruct|limited\s*view|partial\s*view|side\s*stage|"
    r"rear\s*stage|behind\s*stage|accessible|wheelchair", re.I)


def _get(url, timeout=45):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def extract_keys(page_url):
    """Pull workspaceKey, event key and the price table out of a checkout page.

    These are public by necessity -- the chart cannot render without them."""
    raw = _get(page_url)
    ws = re.search(r'workspaceKey\s*:\s*["\']([^"\']+)["\']', raw)
    ev = re.search(r'\bevent\s*:\s*["\']([A-Za-z0-9\-_]{8,})["\']', raw)
    if not (ws and ev):
        return None
    # Currency from the page's own price formatter, not guessed from magnitude.
    # A GBP event was being printed as dollars, which is exactly the kind of
    # error that gets a work order executed wrong.
    cur = "$"
    fm = re.search(r'priceFormatter[^}]{0,400}?return\s*[\'"]([^\'"]{1,3})[\'"]',
                   raw, re.S)
    if fm:
        cur = fm.group(1)
    elif re.search(r'GBP|&pound;|£', raw):
        cur = "£"
    pricing = []
    m = re.search(r'pricing\s*:\s*(\[.*?\])\s*[,\}]', raw, re.S)
    if m:
        try:
            pricing = json.loads(m.group(1))
        except Exception:
            pricing = []
    maxsel = re.search(r'maxSelectedObjects\s*:\s*(\d+)', raw)
    return {
        "page": page_url,
        "workspace_key": ws.group(1),
        "event_key": ev.group(1),
        # category label -> price. The chart returns category keys, not money.
        "prices": {p.get("category"): p.get("price")
                   for p in pricing if isinstance(p, dict)},
        "max_per_order": int(maxsel.group(1)) if maxsel else None,
        "currency": cur,
    }


def rendering_info(workspace_key, event_key):
    last = None
    for host in RENDER_HOSTS:
        url = (f"{host}/system/public/{workspace_key}"
               f"/rendering-info?event_key={event_key}")
        try:
            return json.loads(_get(url))
        except Exception as e:
            last = e
    raise RuntimeError(f"rendering-info failed: {last}")


def parse_label(label):
    """'Balcony-A-6' -> ('Balcony', 'A', 6). 'Balcony Box A-A-1' ->
    ('Balcony Box A', 'A', 1).

    Sections contain hyphens, so split from the RIGHT: the last two components
    are row and seat. Anything that doesn't fit (GA areas, tables) returns a
    seat of None and is reported as an un-numbered area rather than dropped."""
    parts = label.rsplit("-", 2)
    if len(parts) == 3 and parts[2].isdigit():
        return parts[0], parts[1], int(parts[2])
    return label, "", None


def inventory(page_url=None, workspace_key=None, event_key=None, prices=None):
    """Every seat released for sale, with its price band.

    Returns {'seats': [...], 'by_category': {...}, 'total': n, 'max_per_order': n}
    """
    keys = None
    if page_url:
        keys = extract_keys(page_url)
        if not keys:
            raise RuntimeError(f"no seats.io keys found on {page_url}")
        workspace_key = keys["workspace_key"]
        event_key = keys["event_key"]
        prices = keys["prices"]
    prices = prices or {}

    info = rendering_info(workspace_key, event_key)
    cat_label = {str(c["key"]): c.get("label") for c in info.get("categories", [])}
    obj_cat = info.get("objectCategories") or {}
    cfg = (info.get("forSaleConfigsPerEvent") or {}).get(event_key) or {}
    objects = cfg.get("objects") or []

    seats, by_cat = [], defaultdict(list)
    for label in objects:
        cat = cat_label.get(str(obj_cat.get(label, "")), "uncategorised")
        section, row, num = parse_label(label)
        rec = {"label": label, "section": section, "row": row, "seat": num,
               "category": cat, "price": prices.get(cat)}
        seats.append(rec)
        by_cat[cat].append(rec)
    return {
        "event_key": event_key,
        "workspace_key": workspace_key,
        "total": len(seats),
        "seats": seats,
        "by_category": dict(by_cat),
        "max_per_order": (keys or {}).get("max_per_order"),
        "currency": (keys or {}).get("currency", "$"),
        "for_sale": bool(cfg.get("forSale")),
    }


def consecutive_runs(seats, min_len=2):
    """Group seats into runs of adjacent numbers within the same section+row.

    Buyers need seats TOGETHER -- scattered singles are far harder to resell, and
    a work order that says 'any 6 in Band A' can land on six separate seats in
    six rows. Runs are what makes the instruction executable."""
    by_row = defaultdict(list)
    for s in seats:
        if s["seat"] is None:
            continue
        by_row[(s["section"], s["row"])].append(s["seat"])
    runs = []
    for (section, row), nums in by_row.items():
        nums = sorted(set(nums))
        cur = [nums[0]]
        for n in nums[1:]:
            if n == cur[-1] + 1:
                cur.append(n)
            else:
                if len(cur) >= min_len:
                    runs.append((section, row, cur[0], cur[-1], len(cur)))
                cur = [n]
        if len(cur) >= min_len:
            runs.append((section, row, cur[0], cur[-1], len(cur)))
    runs.sort(key=lambda r: -r[4])
    return runs


def work_order(inv, category=None, max_price=None, min_together=2, limit=6):
    """An executable buy instruction: where, how many, at what price.

    `category` picks a band; otherwise the cheapest priced band is used, since
    that is where resale margin usually lives."""
    # Restricted-view and accessible bands are excluded from automatic selection.
    # They are always the cheapest, so price-ranking picks them every time, and
    # they are the seats that resell worst. Ask for one explicitly if you want it.
    bands = {k: v for k, v in inv["by_category"].items()
             if v and v[0].get("price") is not None and not BAD_VIEW.search(k)}
    if category:
        chosen, seats = category, inv["by_category"].get(category, [])
    elif bands:
        chosen = min(bands, key=lambda k: bands[k][0]["price"])
        seats = bands[chosen]
    else:
        chosen, seats = next(iter(inv["by_category"].items()), ("none", []))
        chosen = chosen if seats else "none"
    price = seats[0].get("price") if seats else None
    if max_price is not None and price is not None and price > max_price:
        return {"category": chosen, "price": price, "runs": [],
                "reason": f"cheapest band {price} exceeds max {max_price}"}
    return {
        "category": chosen,
        "price": price,
        "available": len(seats),
        "max_per_order": inv.get("max_per_order"),
        "runs": consecutive_runs(seats, min_together)[:limit],
    }


def format_order(inv, order, title=None, url=None):
    cur = inv.get("currency", "$")
    lines = [f"BUY — {title or inv['event_key']}"]
    if url:
        lines.append(url)
    lines.append(f"  band: {order['category']}  "
                 f"{cur}{order['price']:.2f} ea" if order.get("price")
                 else f"  band: {order['category']}")
    lines.append(f"  {order.get('available', 0)} seats available"
                 + (f", max {order['max_per_order']} per order"
                    if order.get("max_per_order") else ""))
    for section, row, a, b, n in order["runs"]:
        lines.append(f"    {section} row {row}: seats {a}-{b}  ({n} together)")
    if not order["runs"]:
        lines.append(f"    (no consecutive block: "
                     f"{order.get('reason', 'scattered singles only')})")
    return "\n".join(lines)


def _selftest():
    assert parse_label("Balcony-A-6") == ("Balcony", "A", 6)
    assert parse_label("Balcony Box A-A-1") == ("Balcony Box A", "A", 1)
    assert parse_label("Stalls Block H-J-14") == ("Stalls Block H", "J", 14)
    assert parse_label("GA Standing") == ("GA Standing", "", None)
    seats = [{"section": "Balcony", "row": "D", "seat": n} for n in
             [41, 42, 43, 50, 51, 60]]
    runs = consecutive_runs(seats, min_len=2)
    assert runs[0] == ("Balcony", "D", 41, 43, 3), runs
    assert len(runs) == 2, runs           # 60 is a single -> dropped
    print("seatsio.py self-test: ALL PASS\n")


if __name__ == "__main__":
    _selftest()
    URL = ("https://tickets.howtoacademy.com/naomi-klein-tickets/"
           "london-central-hall-westminster/2026-09-30-19-30")
    inv = inventory(page_url=URL)
    print(f"total seats for sale: {inv['total']}  (for_sale={inv['for_sale']})")
    print(f"{'category':<28}{'seats':>7}{'price':>10}")
    print("-" * 46)
    for cat, rows in sorted(inv["by_category"].items(), key=lambda kv: -len(kv[1])):
        p = rows[0].get("price")
        print(f"{cat[:27]:<28}{len(rows):>7}{(f'{p:.2f}' if p else '-'):>10}")
    print()
    print(format_order(inv, work_order(inv), title="Naomi Klein — Central Hall Westminster", url=URL))
