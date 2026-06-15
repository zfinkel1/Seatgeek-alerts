# SeatGeek Price-Drop Alerts

Watches SeatGeek events and texts/emails you when a ticket drops below your target —
so you can grab underpriced tickets to flip. Beats SeatGeek's anti-bot (PerimeterX)
by scraping through **Bright Data's Browser API** (a real remote browser), so it runs
anywhere — including a free GitHub Action 24/7.

## How it works
- A **GitHub Action** runs every 5 min. Each event is throttled to its own `every`
  interval (so a show-today is checked every 5 min; a show-next-month, once a day).
- For each due event it loads the page via Bright Data, captures the live listings
  feed (section + price), and checks them against your threshold.
- New qualifying listings → **email + AT&T text** (deduped: one ping per ticket).

## Watchlist (a Google Sheet)
Columns (headers, any order):

| url | section | threshold | type | every | label | active |
|-----|---------|-----------|------|-------|-------|--------|
| seatgeek event URL | section to watch — **blank = cheapest overall** | number | `$` flat or `%` below avg | `5min`/`15min`/`1h`/`6h`/`daily` (blank=30min) | optional name | `no` to pause |

Example: `…/concert/18076657 | ga field | 1000 | $ | 5min | Rufus GA Field | yes`

**Add an event:** copy its SeatGeek URL into a new row, set a threshold, save. The
Action picks it up next run. Publish the sheet (File → Share → Publish to web → CSV)
and put that CSV link in the `SHEET_CSV_URL` secret. (Or just edit `watchlist.csv` in
the repo.)

## Setup
1. Create a **new GitHub repo** and push this folder. **Make it Public** — Actions
   minutes are unlimited on public repos (private is capped at 2,000 min/mo, which a
   5-min cron blows past). No secrets live in the code, so public is safe.
2. Repo → **Settings → Secrets and variables → Actions** → add:
   - `BRIGHTDATA_BROWSER_WSS` — your Bright Data Browser API endpoint
     (`wss://brd-customer-...@brd.superproxy.io:9222`)
   - `SENDGRID_API_KEY` — (reuse your existing SendGrid key)
   - `FROM_EMAIL` — a SendGrid-verified sender (e.g. your alert email)
   - `ALERT_EMAIL` — where alerts go (zfinkel1@gmail.com)
   - `ALERT_PHONE` — 10-digit number for AT&T text (e.g. `3125551234`)
   - `SHEET_CSV_URL` — published Google-Sheet CSV link (optional; falls back to watchlist.csv)
3. Actions tab → run **SeatGeek Watch** manually once (`workflow_dispatch`) to test.

## Cost (Bright Data, ~$5/GB)
~$0.005–0.01 per check. The per-event `every` column is your cost dial: hammer the
events you're hunting today, leave future ones on `daily` for pennies.

## Local test
```
pip install -r requirements.txt
set BRIGHTDATA_BROWSER_WSS=wss://...   (PowerShell: $env:BRIGHTDATA_BROWSER_WSS="wss://...")
set SENDGRID_API_KEY=...  set ALERT_EMAIL=...  set ALERT_PHONE=...  set FROM_EMAIL=...
python watch.py
```
Uses `watchlist.csv` if `SHEET_CSV_URL` isn't set.
