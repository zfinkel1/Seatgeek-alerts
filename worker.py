"""
Always-on worker for Railway.

GitHub Actions throttled our */5 schedule down to once every few HOURS on the
free public-repo scheduler — useless for catching fast flips. So we run the
watcher in a tight loop on a real always-on host instead.

Each loop calls watch.main(); the per-event "every" column in the watchlist
still controls how often each event is actually SCRAPED (so Bright Data cost is
governed by "every", not by this loop). This loop just needs to tick often
enough to honor the shortest interval you'd set (5min), so we check each minute.
"""
import os
import time
import traceback

from watch import main

LOOP_SECONDS = int(os.environ.get("LOOP_SECONDS", "60"))

print(f"[worker] starting — checking for due events every {LOOP_SECONDS}s", flush=True)
while True:
    try:
        main()
    except Exception as e:
        print(f"[worker] run failed: {e}", flush=True)
        traceback.print_exc()
    time.sleep(LOOP_SECONDS)
