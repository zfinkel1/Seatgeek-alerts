"""
Real zones: contiguous groups of sections that are actually comparable seats.

WHY NOT "THE 300 LEVEL"
Lumping every 300-level section together is the same mistake as watch.py's +-3
section rule, just at a coarser grain. At a rink, 300-level center ice and
300-level behind-the-goal are different products at different prices. A zone has
to be a *run* of adjacent sections that share a price level -- "the three sections
either side of center," not "everything upstairs."

HOW THIS FINDS THEM WITHOUT A SEATMAP
Section numbers run sequentially around a bowl, so numeric order is physical
order. Walk the sections in that order and cut wherever the price steps sharply.
What's left between cuts are contiguous, price-coherent runs -- which is what a
zone is.

The bowl is a CIRCLE (the last section neighbours the first), so segmentation
wraps. Missing numbers are treated as real gaps, since a missing section usually
means a physical break -- a tunnel, a camera well, the stage.

This needs no venue map and no vendor taxonomy, and it adapts to the event: for
hockey the premium run lands at center ice, for an end-stage concert it lands
facing the stage. Same code, different answer, because the prices differ.

INPUT PRICES
Prefer median SALE price per section. Asks are inflated and noisy -- this event's
median ask was $795 while buyers paid $259 -- but their *relative ordering* across
sections still carries seat-quality information, so asks are an acceptable
fallback when sales are too thin, which they usually are per-section.
"""
import re
import statistics

_SEC_RE = re.compile(r"^\s*(?:sec(?:tion)?\s*)?([a-z]*)\s*(\d+)\s*([a-z]*)\s*$", re.I)


def parse_section(s):
    """('', 318) for '318' / 'Section 318'; (prefix, n) for lettered variants.
    None for named areas (GA, Floor, Loge) which have no bowl position."""
    m = _SEC_RE.match(str(s or ""))
    if not m:
        return None
    pre, num, suf = m.group(1) or "", m.group(2), m.group(3) or ""
    return ((pre + suf).lower(), int(num))


def level_of(num):
    """Bowl level from the section number: 318 -> 300. Levels are separate rings,
    so they are segmented independently -- 118 and 318 are never one zone."""
    return (num // 100) * 100


def segment(prices, rel_break=0.35, min_run=2):
    """Split one level's sections into contiguous zones.

    prices: {section_number: representative_price}
    rel_break: fractional step between neighbours that counts as a boundary.
    min_run: runs shorter than this get merged into the cheaper neighbour, so a
             single odd-priced section doesn't become its own "zone".

    Returns {section_number: zone_index}, zone 0 being the priciest run.
    """
    nums = sorted(prices)
    if len(nums) < 2:
        return {n: 0 for n in nums}

    # Boundary between consecutive sections when the price steps hard OR the
    # numbering skips (a gap in the ring is a physical break).
    cuts = set()
    for i in range(len(nums)):
        a, b = nums[i], nums[(i + 1) % len(nums)]
        pa, pb = prices[a], prices[b]
        gap = (b - a) not in (1, 1 - len(nums)) and b != a + 1
        step = abs(pb - pa) / max(min(pa, pb), 1e-9)
        if step >= rel_break or (gap and b != nums[0]):
            cuts.add(i)

    if len(cuts) >= len(nums):        # every edge cut -> no structure to find
        return {n: 0 for n in nums}
    if not cuts:
        return {n: 0 for n in nums}

    # Walk the circle from just after a cut, starting a new run at each cut.
    start = (max(cuts) + 1) % len(nums)
    runs, cur = [], []
    for k in range(len(nums)):
        i = (start + k) % len(nums)
        cur.append(nums[i])
        if i in cuts:
            runs.append(cur)
            cur = []
    if cur:
        runs.append(cur)

    # Absorb runs that are too short to be a real zone.
    merged = True
    while merged and len(runs) > 1:
        merged = False
        for i, r in enumerate(runs):
            if len(r) < min_run:
                j = (i - 1) % len(runs)
                k = (i + 1) % len(runs)
                tgt = j if statistics.mean(prices[x] for x in runs[j]) < \
                    statistics.mean(prices[x] for x in runs[k]) else k
                runs[tgt] = runs[tgt] + r
                runs.pop(i)
                merged = True
                break

    # Merge runs that sit at the same PRICE LEVEL even when they aren't adjacent.
    # A rink has center ice on both sides of the bowl: two separate runs, one kind
    # of seat. Keeping them apart would halve the sample in each and defeat the
    # point of zoning, which is pooling comparable seats until there's enough data
    # to see anything.
    runs.sort(key=lambda r: -statistics.mean(prices[x] for x in r))
    tiers = []                       # [[run, ...], ...] grouped by price level
    for r in runs:
        rp = statistics.mean(prices[x] for x in r)
        for t in tiers:
            tp = statistics.mean(prices[x] for run in t for x in run)
            if abs(rp - tp) / max(min(rp, tp), 1e-9) < rel_break:
                t.append(r)
                break
        else:
            tiers.append([r])
    return {n: zi for zi, t in enumerate(tiers) for r in t for n in r}


def build(section_prices, **kw):
    """Zone map across every level. Returns {section_label: 'L300-Z0', ...}."""
    by_level = {}
    for label, price in section_prices.items():
        p = parse_section(label)
        if not p or price is None:
            continue
        _, num = p
        by_level.setdefault(level_of(num), {})[num] = float(price)

    out = {}
    for lvl, prices in by_level.items():
        for num, zi in segment(prices, **kw).items():
            out[num] = f"L{lvl}-Z{zi}"
    return out


def describe(zone_map, section_prices):
    """Human-readable summary: each zone, its sections, its price band."""
    groups = {}
    for num, z in zone_map.items():
        groups.setdefault(z, []).append(num)
    lines = []
    for z in sorted(groups):
        secs = sorted(groups[z])
        ps = [section_prices[s] for s in secs if section_prices.get(s)]
        band = f"{min(ps):.0f}-{max(ps):.0f}" if ps else "n/a"
        run = ",".join(str(s) for s in secs)
        lines.append(f"  {z:<10} {len(secs):>2} sections  ${band:<12} {run}")
    return "\n".join(lines)


def _selftest():
    print("zones.py self-test\n" + "-" * 60)
    assert parse_section("318") == ("", 318)
    assert parse_section("Section 318") == ("", 318)
    assert parse_section("GA Floor") is None
    assert level_of(318) == 300 and level_of(118) == 100

    # Synthetic rink: 12 sections around a ring. 2 center-ice runs (premium) on
    # opposite sides, corners cheap. Zone 0 must recover the premium sections.
    prices = {301: 900, 302: 950, 303: 920,      # center, side A
              304: 400, 305: 380, 306: 390,      # corner/end
              307: 910, 308: 940, 309: 900,      # center, side B
              310: 395, 311: 385, 312: 405}      # corner/end
    zmap = segment(prices)
    print(describe({k: f"L300-Z{v}" for k, v in zmap.items()}, prices))
    prem = {s for s, z in zmap.items() if z == 0}
    assert prem == {301, 302, 303, 307, 308, 309}, prem
    assert len({z for z in zmap.values()}) == 2, "expected premium + cheap"

    # A level with no structure must not be shredded into fake zones.
    flat = {n: 500 + (n % 3) for n in range(201, 213)}
    assert len(set(segment(flat).values())) == 1, "flat level should be one zone"
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
