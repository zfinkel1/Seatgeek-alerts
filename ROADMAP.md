# Roadmap

## The end product

Not a dashboard. Not a signal. A **work order** — an instruction a buyer with no
market context can execute without asking a question, handed to a team of overseas
buyers holding many accounts (so per-account purchase limits aren't a constraint).

Every design decision gets judged against: *could someone with no context execute
this correctly?*

Three parts, in priority order.

---

## Part 1 — Niche primary buy orders  ← **STARTING HERE**

> "Andy Cohen at Cooper Union. Buy these seats at this price. List at this price."

Off-radar primary sites selling seated events at face, where a real secondary
market exists. The margin is visible at purchase — no forecasting, no vendor data.

### Status

| piece | state |
|---|---|
| crawl a niche site, extract events | **working** (`sources.py`, proven on 2,792 Tao events) |
| price ladder + sold-out tiers | **working** — schema.org JSON-LD carries tier prices and stock |
| "does it have a secondary market" filter | **partly** — SeatGeek venue lookup works but fuzzy-matches badly |
| secondary value estimate | **partly** (`primary.py`) — comps must be venue-type matched, see below |
| exact section / row / seat | **not built** — needs per-site seat inventory |
| alerting | **exists** (`alerts.py`, Telegram + email) |

### Known bugs to fix
- **Comparable selection.** Scoring Zedd's Vegas club GA against his arena dates
  produced a fake +406%. Comps must match venue type, or better, match the
  *specific event* on secondary.
- **Venue matching.** SeatGeek's fuzzy search mapped "Marquee New York" to MetLife
  Stadium. Needs exact name + city matching.

### Target categories
Evidence says **seated events only**. Reserved seating creates resale; general
admission doesn't. Vegas nightclubs measured 0–3 upcoming SeatGeek events —
effectively no secondary market, so Tao Group is largely the wrong target despite
being the first site built.

Go after: talks/lectures (How To Academy, 92nd St Y, Live Talks), comedy, podcast
live shows, indie seated venues (Etix, Prekindle, ShowClix, AudienceView,
Ticket Tailor). Not nightlife — wrong product for resale.

---

## Part 2 — Secondary buy orders

> "Buy section 118 row 12 on SeatGeek at $85. Sell at $140."

Needs to know what a specific seat is worth, which needs real sales history.
**Blocked on DataIQ** (or another exportable source).

Built and unit-tested, waiting on data:
- `signals.py` — supply, sell-through, price trend, sold-vs-pulled attribution
- `rules.py` — 8 candidate buy rules + controls
- `backtest.py` — walk-forward, look-ahead-proof, 8% fee, markdown-not-writeoff exits
- `zones.py` — real zones (center-ice vs corner, not "the 300 level")
- `ticketgenie.py`, `tg_pull.py`, `tg_probe.py` — TicketGenie client and corpus puller

---

## Part 3 — News-driven alerts

> "Top prospect debuts Thursday for the Iowa Cubs. Tickets are $8. Here's the link."

A trigger in the world implies a ticket buy. **Not a pricing problem** — a
trigger-to-event mapping problem, and completely separate machinery from Parts 1
and 2. Needs no vendor data.

- **Obscure triggers = the real edge.** Prospect call-ups are public (MLB
  transactions, Baseball America) but nobody connects them to thin, cheap ticket
  markets.
- **Marquee triggers = unwinnable.** LeBron's final game moves in minutes and
  everyone knows. Milestones are still computable from public stats and schedules,
  but assume no edge on speed.

---

## Open questions

1. **Seat-level detail** — do buyers need exact section/row/seat, or is "best
   available in the balcony under $65, here's the link" enough to execute? The
   second is far cheaper to build.
2. **Does DataIQ export?** Gates all of Part 2.
3. **Transferability** — will-call or non-transferable delivery kills resale
   regardless of margin. Check per source before building a crawler for it.
