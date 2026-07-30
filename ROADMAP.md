# Roadmap

## The business

Buy tickets from sites nobody watches. Resell them. Do it at a scale that only
works with software.

The proof it works: an Andy Cohen event on How To Academy, found by hand, returned
~$1,000. The whole system is an attempt to do that a hundred times over without
needing to be the person who happens to recognise the name.

## The output

Not a dashboard, not a score. A **work order** — an instruction an overseas buyer
with no market context can execute without asking a question:

```
BUY — Andy Cohen, Cooper Union, Oct 20
  Band C  $49.95 ea, max 6 per account
  Balcony row G: seats 88-113  (26 together)
  → link
  List at: ~$150
```

Every design decision gets judged against: *could someone with no context execute
this correctly?*

---

# PART 1 — Niche primary  ← the main line

Buy at face from operators who don't price to market, resell on secondary.
No forecasting: the margin is visible at purchase.

### 1. Discovery — find the events

The bottleneck. Doing this venue-by-venue never finishes; the leverage is going
**platform** by platform, because one platform covers hundreds of venues.

| piece | status |
|---|---|
| List every site on a platform (PublicWWW: search `cdn.seatsio.net`) | **not built** — needs an account, ~$50–100 |
| Crawl one site's event list | **partial** — works on How To Academy |
| Crawl the rest | **not built** — a chunk of work per site; the messy part |
| Detect new events (don't re-alert) | **built** (`scout.py`) |

Target platforms after seats.io: Tixr, Spektrix, Ticketsolve, AudienceView, Etix,
Prekindle, ShowClix.

### 2. Qualification — is it worth looking at?

| piece | status |
|---|---|
| Is the performer notable? | **built** — Wikipedia pageviews (cold-start signal; replace with real demand data) |
| Does this venue's inventory resell at all? | **partial** — seated events do, GA club nights measurably don't |
| Are tickets transferable? | **not built** — will-call kills resale regardless of margin |
| US only | **built** — can't sell outside the USA |

### 3. Valuation — what will it sell for?

A lookup, not a forecast: what does this performer's inventory actually go for.

| piece | status |
|---|---|
| Look up performer's secondary prices | **built** (`primary.py`, free SeatGeek API) |
| Fit the multiple over face | **built** (`pairing.py`) — needs volume to be meaningful |
| Use **sold** prices instead of asks | **blocked on DataIQ** — asks run far above sales ($795 vs $259 measured) |
| Estimate for performers with no history | **not built** — needs the labeled dataset |

### 4. The work order — exactly what to buy

| piece | status |
|---|---|
| Seat-level inventory (section/row/seat, price bands) | **built** (`seatsio.py`) — works on any seats.io venue |
| Consecutive-seat blocks | **built** — scattered singles are hard to resell |
| Exclude restricted-view / accessible | **built** |
| Max price, quantity, link | **built** |
| Same for non-seats.io platforms | **not built** — one reader per platform |

### 5. Alerting

**built** — `alerts.py` (Telegram + email) has been running for months.

### 6. Feedback loop

| piece | status |
|---|---|
| Record every buy: paid, listed, sold, days held | **not built** — this is the training set nobody else has |

---

# PART 2 — Secondary directional

Buy listings that will appreciate. Needs prediction, history, and a data vendor.
Competes against Automatiq-armed brokers on events everyone can see.

Built and unit-tested, waiting on sold data:

- `signals.py` — supply, sell-through, price trend, sold-vs-pulled attribution
- `rules.py` — 8 candidate rules + buy-everything / buy-nothing controls
- `backtest.py` — walk-forward, structurally look-ahead-proof, 8% fee, models
  markdown exits rather than scoring unsold as a total loss
- `zones.py` — contiguous price-coherent zones (centre-ice vs corner)
- `ticketgenie.py` / `tg_pull.py` / `tg_probe.py` — client, corpus puller, probe

**Nothing here is validated.** It is a scoreboard with nothing on it.

### Supporting pieces

- **Mirror detection** (`pairing.py`, crude) — brokers list tickets they don't own
  and buy only if they sell. Those listings are not demand, and they poison any
  valuation built on asks. Real version needs listing-level secondary data.
- **Primary depletion** (`depletion.py`) — watches seats.io inventory drain and
  flags events on pace to sell out. The cleanest signal available: a seat leaving
  the primary chart is *sold*, with none of secondary's ambiguity. Needs a second
  snapshot to verify its own premise.

---

# PART 3 — News-driven alerts

A trigger in the world implies a ticket buy. Not a pricing problem — a
trigger-to-event mapping problem, and separate machinery from Parts 1 and 2.
Needs no vendor data. **Not started.**

- **Obscure triggers are the edge.** A top prospect's minor-league debut is public
  information (MLB transactions, Baseball America) that nobody connects to thin,
  cheap ticket markets.
- **Marquee triggers are unwinnable.** LeBron's final game moves in minutes.
  Milestones are computable from public stats and schedules, but assume no edge
  on speed.

---

## What's actually true today

**Working end to end:** `scout.py` crawls a site, checks notability, and texts you.
On How To Academy US it found 4 of 14 events — including the Andy Cohen event that
made $1,000, and Chuck Lorre, who has no resale market yet.

**The big unknown:** whether Andy Cohen was luck or a pattern. One trade. No amount
of code answers that — only more trades do.

## Order of work

1. Get the site list (PublicWWW) — turns 1 site into hundreds
2. Build event discovery per site — the grind
3. Start recording every buy — the training set compounds from day one
4. Swap real sold prices in when DataIQ lands
5. Part 3 whenever; it's independent of everything above

## Open questions

1. **Does DataIQ export?** Gates all of Part 2 and the accuracy of Part 1.
2. **Does the niche primary edge generalise?** Needs ~10 more trades to know.
3. **Transferability per source** — check before building any crawler.
