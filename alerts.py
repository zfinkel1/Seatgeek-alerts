"""
Alert delivery — email + text (AT&T email-to-SMS) via SendGrid.
Text is just an email to <number>@txt.att.net, so one SendGrid call covers both.
"""
import os
import json
import urllib.request

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL")          # zfinkel1@gmail.com
ALERT_PHONE = os.environ.get("ALERT_PHONE")          # 10-digit, e.g. 3125551234 (AT&T)
FROM_EMAIL = os.environ.get("FROM_EMAIL", ALERT_EMAIL)  # verified SendGrid sender

ATT_SMS_GATEWAY = "txt.att.net"


def _send(to_addr, subject, body):
    if not (SENDGRID_API_KEY and FROM_EMAIL and to_addr):
        print(f"  [alert] missing creds, would send to {to_addr}: {subject}")
        return
    payload = {
        "personalizations": [{"to": [{"email": to_addr}]}],
        "from": {"email": FROM_EMAIL},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }
    req = urllib.request.Request(
        "https://api.sendgrid.com/v3/mail/send",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {SENDGRID_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            print(f"  [alert] sent to {to_addr} ({r.status})")
    except Exception as e:
        print(f"  [alert] send failed to {to_addr}: {e}")


def send_alert(label, event_url, listing, limit):
    """Fire an email + text for one qualifying listing."""
    sec = listing["section"] or "any section"
    price = listing["price"]
    qty = listing.get("qty")
    row = listing.get("row")
    resale = listing.get("resale")
    qty_txt = f" · {qty} avail" if qty else ""
    row_txt = f" row {row}" if row else ""

    if resale:
        # flip: show the section's real going rate + projected net profit after fees
        fee = float(os.environ.get("FLIP_FEE_PCT", "15")) / 100.0
        net = resale * (1 - fee)
        profit = net - price
        pct = listing.get("flip_pct", (profit / price * 100) if price else 0)
        subject = f"🎟️ {label}: {sec}{row_txt} ${price:.0f} → ~${resale:.0f} mkt (+{pct:.0f}%)"
        body = (
            f"{label}\n"
            f"{sec}{row_txt} — BUY ${price:.0f}{qty_txt}\n"
            f"section going rate ~${resale:.0f}; resell − {fee*100:.0f}% fee = ${net:.0f} net\n"
            f"→ profit +${profit:.0f} ({pct:.0f}%)\n\n"
            f"BUY: {event_url}\n"
        )
        sms_body = f"{label}: {sec}{row_txt} ${price:.0f} (mkt ~${resale:.0f}, +{pct:.0f}%)\n{event_url}"
    else:
        ref = f" (≤ ${limit:.0f})" if limit else ""
        subject = f"🎟️ {label}: {sec}{row_txt} ${price:.0f}{ref}"
        body = (
            f"{label}\n"
            f"{sec}{row_txt} — ${price:.2f}{qty_txt}\n"
            + (f"(buy-line ${limit:.2f})\n\n" if limit else "\n")
            + f"BUY: {event_url}\n"
        )
        sms_body = f"{label}: {sec}{row_txt} ${price:.0f}{qty_txt}\n{event_url}"

    if ALERT_EMAIL:
        _send(ALERT_EMAIL, subject, body)
    if ALERT_PHONE:
        sms_to = f"{ALERT_PHONE}@{ATT_SMS_GATEWAY}"
        _send(sms_to, subject, sms_body)
