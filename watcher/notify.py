"""Send alerts to whichever channels you've set up (any combination):

  ntfy       NTFY_TOPIC (+ optional NTFY_SERVER, default https://ntfy.sh, and NTFY_TOKEN for a
             self-hosted server with access control)
  Telegram   TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID
  WhatsApp   CALLMEBOT_PHONE + CALLMEBOT_APIKEY (free CallMeBot relay, personal use)

With none set, alerts are just printed in the GitHub Actions log.
"""
import json
import os
import sys
import urllib.parse
import urllib.request

UA = {"User-Agent": "crypto-coach-watcher/1.0"}


def _post(url, data, headers):
    req = urllib.request.Request(url, data=data, headers=dict(UA, **headers))
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def channels():
    e = os.environ
    out = []
    if e.get("NTFY_TOPIC"):
        out.append("ntfy")
    if e.get("TELEGRAM_BOT_TOKEN") and e.get("TELEGRAM_CHAT_ID"):
        out.append("telegram")
    if e.get("CALLMEBOT_PHONE") and e.get("CALLMEBOT_APIKEY"):
        out.append("whatsapp")
    return out


def send_ntfy(text, priority):
    e = os.environ
    server = (e.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    body = {"topic": e["NTFY_TOPIC"], "message": text, "title": "Crypto Coach",
            "priority": priority, "tags": ["chart_with_upwards_trend"]}
    headers = {"Content-Type": "application/json"}
    if e.get("NTFY_TOKEN"):
        headers["Authorization"] = "Bearer " + e["NTFY_TOKEN"]
    _post(server + "/", json.dumps(body).encode("utf-8"), headers)   # JSON publish: emoji-safe


def send_telegram(text, priority):
    e = os.environ
    body = urllib.parse.urlencode({"chat_id": e["TELEGRAM_CHAT_ID"], "text": text}).encode()
    _post(f"https://api.telegram.org/bot{e['TELEGRAM_BOT_TOKEN']}/sendMessage", body, {})


def send_whatsapp(text, priority):
    e = os.environ
    q = urllib.parse.urlencode({"phone": e["CALLMEBOT_PHONE"], "text": text, "apikey": e["CALLMEBOT_APIKEY"]})
    req = urllib.request.Request("https://api.callmebot.com/whatsapp.php?" + q, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


SENDERS = {"ntfy": send_ntfy, "telegram": send_telegram, "whatsapp": send_whatsapp}


def send_all(alerts):
    """alerts: [(text, priority)]. One failing channel never stops the others."""
    chans = channels()
    for text, priority in alerts:
        print(f"[alert p{priority}] {text}")
        for ch in chans:
            try:
                SENDERS[ch](text, priority)
            except Exception as ex:  # noqa: BLE001
                print(f"{ch} failed: {ex}", file=sys.stderr)
    if not chans and alerts:
        print("(no alert channel set up: add NTFY_TOPIC, Telegram or CallMeBot secrets)")
