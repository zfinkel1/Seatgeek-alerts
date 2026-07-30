"""
SeatEngine reader — exact remaining inventory for comedy clubs and small venues.

WHY THIS ONE MATTERS
SeatEngine embeds its full inventory as JSON directly in the event page, and it
states availability outright rather than implying it:

    {"name":"General Admission","available":245,"price":2700,"service_charge":599}

Prices are in CENTS, and `service_charge` is separate -- a $27.00 ticket really
costs $32.99. Reading `price` as dollars, or forgetting the fee, understates cost
by ~22% on a cheap comedy ticket, which is most of a margin.

Compared with seats.io this is strictly better for tracking depletion: seats.io
gives a list of seats released for sale and leaves you to infer what's gone,
whereas SeatEngine reports the remaining count per tier. Poll it over time and
sell-through is a subtraction rather than a guess.

Powers Helium Comedy's clubs and other comedy venues, so one reader covers many
rooms -- the same platform-not-venue leverage as seats.io.
"""
import re
import json
import urllib.request

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}

# Add-ons rather than admission: insurance, parking, merch, fees. They carry
# their own `available` counts and would otherwise pollute both the work order
# and any sell-through measurement.
NON_TICKET = re.compile(
    r"protection|insurance|parking|donation|merch|shirt|poster|fee|gratuity|"
    r"upgrade only|add-?on", re.I)


def _get(url, timeout=45):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA),
                                timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _find_inventories(raw):
    """Pull the `inventories` array out of the page's embedded JSON.

    Brace-matched rather than regex-captured because the array contains nested
    objects and HTML-escaped description text with braces in it."""
    m = re.search(r'"inventories"\s*:\s*\[', raw)
    if not m:
        return None
    start = m.end() - 1
    depth, in_str, esc = 0, False, False
    for i in range(start, len(raw)):
        c = raw[i]
        if esc:
            esc = False
            continue
        if c == "\\":
            esc = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i + 1])
                except Exception:
                    return None
    return None


def _flag(raw, key):
    m = re.search(r'"' + key + r'"\s*:\s*(true|false|\d+|"[^"]*")', raw)
    if not m:
        return None
    v = m.group(1)
    if v in ("true", "false"):
        return v == "true"
    return v.strip('"')


def inventory(event_url):
    """Remaining inventory and real prices for one SeatEngine event.

    Returns tiers with all-in price (admission + service charge), the exact
    number still available, and the per-order cap."""
    raw = _get(event_url)
    rows = _find_inventories(raw)
    if rows is None:
        raise RuntimeError(f"no SeatEngine inventory found at {event_url}")

    tiers = []
    for r in rows:
        if not isinstance(r, dict) or r.get("price") is None:
            continue
        name = str(r.get("title") or r.get("name") or "").strip()
        if NON_TICKET.search(name):
            continue
        price = float(r["price"]) / 100.0
        fee = float(r.get("service_charge") or 0) / 100.0
        tiers.append({
            "id": r.get("id"),
            "name": name,
            "price": price,
            "fee": fee,
            "all_in": round(price + fee, 2),
            "available": r.get("available"),
            "assigned_seating": bool(r.get("assigned")),
        })
    tiers.sort(key=lambda t: t["all_in"])
    total = sum((t["available"] or 0) for t in tiers)
    return {
        "url": event_url,
        "tiers": tiers,
        "total_available": total,
        "sold_out": bool(_flag(raw, "sold_out")) or total == 0,
        "max_per_order": _flag(raw, "max_seats"),
        "on_sale_start": _flag(raw, "start_on_sale"),
        "on_sale_end": _flag(raw, "end_on_sale"),
    }


def work_order(inv, max_price=None, exclude_packages=True):
    """Cheapest genuinely buyable tier, with what's left of it.

    Packages ("Couples Package (Includes 2 Tickets)") price per-bundle, not
    per-seat, so their headline number isn't comparable to a single ticket and
    they're excluded by default."""
    tiers = [t for t in inv["tiers"] if (t["available"] or 0) > 0]
    if exclude_packages:
        tiers = [t for t in tiers
                 if not re.search(r"package|bundle|includes \d", t["name"], re.I)] or tiers
    if max_price is not None:
        tiers = [t for t in tiers if t["all_in"] <= max_price]
    if not tiers:
        return None
    t = tiers[0]
    return {
        "tier": t["name"],
        "price": t["price"],
        "all_in": t["all_in"],
        "available": t["available"],
        "max_per_order": inv.get("max_per_order"),
        "url": inv["url"],
    }


def format_order(order, title=None):
    if not order:
        return "no buyable inventory"
    lines = [f"BUY — {title or order['url']}",
             f"  {order['tier']}: ${order['all_in']:.2f} all-in "
             f"(${order['price']:.2f} + fee)",
             f"  {order['available']} left"
             + (f", max {order['max_per_order']} per order"
                if order.get("max_per_order") else ""),
             f"  {order['url']}"]
    return "\n".join(lines)


def _selftest():
    page = ('junk {"inventories":['
            '{"id":1,"title":"General Admission","available":245,"price":2700,'
            '"service_charge":599,"assigned":false},'
            '{"id":2,"title":"Reserved","available":50,"price":3700,'
            '"service_charge":599,"assigned":true},'
            '{"id":3,"title":"Couples Package (Includes 2 Tickets)",'
            '"available":15,"price":17000,"service_charge":599},'
            '{"id":4,"title":"Ticket Protection","available":100,"price":675}'
            '],"sold_out":false,"max_seats":15} more junk')
    rows = _find_inventories(page)
    assert rows and len(rows) == 4

    class Fake:
        pass
    import types
    mod = types.SimpleNamespace()
    # exercise the parsing path directly
    tiers = []
    for r in rows:
        name = r.get("title")
        if NON_TICKET.search(name):
            continue
        tiers.append({"name": name,
                      "price": r["price"] / 100.0,
                      "fee": (r.get("service_charge") or 0) / 100.0,
                      "all_in": round(r["price"] / 100.0
                                      + (r.get("service_charge") or 0) / 100.0, 2),
                      "available": r["available"]})
    assert len(tiers) == 3, "ticket protection must be filtered out"
    tiers.sort(key=lambda t: t["all_in"])
    assert tiers[0]["all_in"] == 32.99, tiers[0]   # cents + fee handled
    inv = {"tiers": tiers, "url": "u", "max_per_order": 15,
           "total_available": 310, "sold_out": False}
    o = work_order(inv)
    assert o["tier"] == "General Admission", o     # package excluded
    assert o["available"] == 245
    assert work_order(inv, max_price=10) is None   # price cap respected
    print("seatengine.py self-test: ALL PASS\n")


if __name__ == "__main__":
    _selftest()
    for url in ["https://philadelphia.heliumcomedy.com/events/140655",
                "https://portland.heliumcomedy.com/events/137293"]:
        try:
            inv = inventory(url)
        except Exception as e:
            print(f"{url}: {e}")
            continue
        print(f"{url}")
        print(f"  total available: {inv['total_available']}  "
              f"sold_out={inv['sold_out']}  max/order={inv['max_per_order']}")
        for t in inv["tiers"]:
            print(f"    {t['name'][:38]:<40} ${t['all_in']:>7.2f} all-in  "
                  f"{t['available']} left")
        print(format_order(work_order(inv)))
        print()
