"""
Alert delivery — email via SendGrid + real SMS via Twilio.
(The AT&T email-to-SMS gateway is dead, so texts go through Twilio when configured.)
"""
import os
import re
import json
import base64
import urllib.parse
import urllib.request

SENDGRID_API_KEY = os.environ.get("SENDGRID_API_KEY")
ALERT_EMAIL = os.environ.get("ALERT_EMAIL")          # zfinkel1@gmail.com
ALERT_PHONE = os.environ.get("ALERT_PHONE")          # 10-digit or +1...; the destination
FROM_EMAIL = os.environ.get("FROM_EMAIL", ALERT_EMAIL)  # verified SendGrid sender

ATT_SMS_GATEWAY = "txt.att.net"

# Twilio — real SMS straight to the phone (instant, no spam folder, no dead gateway).
TWILIO_SID = os.environ.get("TWILIO_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_FROM = os.environ.get("TWILIO_FROM")          # your Twilio number, e.g. +13125551234

# Telegram — free instant push to your phone. The SMS gateway is dead and Twilio
# needs A2P registration, so Telegram is the practical phone-alert channel.
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")


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


def _send_sms(body):
    """Real SMS via Twilio if configured; else fall back to the (defunct) carrier
    email-to-SMS gateway. Twilio is instant and actually delivers."""
    if not ALERT_PHONE:
        return
    to = ALERT_PHONE if ALERT_PHONE.startswith("+") else "+1" + ALERT_PHONE
    if TWILIO_SID and TWILIO_AUTH_TOKEN and TWILIO_FROM:
        data = urllib.parse.urlencode({"From": TWILIO_FROM, "To": to, "Body": body}).encode()
        req = urllib.request.Request(
            f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_SID}/Messages.json",
            data=data, method="POST",
        )
        auth = base64.b64encode(f"{TWILIO_SID}:{TWILIO_AUTH_TOKEN}".encode()).decode()
        req.add_header("Authorization", "Basic " + auth)
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                print(f"  [alert] Twilio SMS sent ({r.status})")
        except Exception as e:
            print(f"  [alert] Twilio SMS failed: {e}")
    else:
        _send(f"{ALERT_PHONE}@{ATT_SMS_GATEWAY}", "ticket alert", body)


def _telegram_api(method, params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/{method}",
        data=data, method="POST",
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def event_id_from_url(url):
    """Stable event id for muting — the 6+ digit id in the SeatGeek URL.
    Must match watch.event_id() so a mute keys to the same event."""
    m = re.search(r"(\d{6,})", url or "")
    return m.group(1) if m else (url or "").strip()


def mute_key(event_id, section):
    """The (event, section) key a mute targets. Section lowercased + capped so the
    whole callback_data stays under Telegram's 64-byte limit, and so the worker
    matches future listings to the mute consistently."""
    return f"{event_id}|{(section or '').strip().lower()[:48]}"


def _send_telegram(body, mute_target=None):
    """Instant push to the phone via a Telegram bot (free, reliable).
    mute_target = a mute_key() string -> attach a 'Mute this section' button."""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return
    params = {"chat_id": TELEGRAM_CHAT_ID, "text": body}
    if mute_target:
        params["reply_markup"] = json.dumps({"inline_keyboard": [[
            {"text": "🚫 Mute this section", "callback_data": "ig|" + mute_target}
        ]]})
    try:
        _telegram_api("sendMessage", params)
        print("  [alert] Telegram sent")
    except Exception as e:
        print(f"  [alert] Telegram failed: {e}")


def poll_ignores(state):
    """Pull 'Mute this section' taps from Telegram and record them in
    state['ignored']. Advances state['tg_offset'] so each tap is read once.
    Returns the number of NEW sections muted this poll."""
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return 0
    offset = state.get("tg_offset", 0)
    url = (f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
           f"?timeout=0&offset={offset}"
           f"&allowed_updates={urllib.parse.quote('[\"callback_query\"]')}")
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        print(f"  [ignore] poll failed: {e}")
        return 0
    ignored = set(state.get("ignored", []))
    new = 0
    for upd in data.get("result", []):
        state["tg_offset"] = upd["update_id"] + 1
        cq = upd.get("callback_query")
        if not cq:
            continue
        cdata = cq.get("data", "")
        if cdata.startswith("ig|"):
            key = cdata[3:]
            if key not in ignored:
                ignored.add(key)
                new += 1
            try:
                _telegram_api("answerCallbackQuery", {
                    "callback_query_id": cq["id"],
                    "text": "Muted ✓ — won't alert this section again",
                })
            except Exception:
                pass
    state["ignored"] = sorted(ignored)
    return new


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
    _send_telegram(body, mute_target=mute_key(event_id_from_url(event_url), listing["section"]))
    _send_sms(sms_body)
