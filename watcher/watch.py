#!/usr/bin/env python3
"""Crypto Coach watcher.

Pulls free public data from Kraken, scores the top-warning flags and the
buy signals for every coin in coins.json, tracks the dip ladder, writes
site/signals.json for the app, and sends a Telegram alert only when
something changes. Standard library only - no installs needed.

Usage:  python watcher/watch.py [--config coins.json] [--out site/signals.json]
Env:    PREV_URL            URL of the last published signals.json (for memory between runs)
        TELEGRAM_BOT_TOKEN  optional, for alerts
        TELEGRAM_CHAT_ID    optional, for alerts
        DEMO_OFFLINE=1      use synthetic candles (for testing without network)
"""
import argparse
import json
import math
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

SPOT = "https://api.kraken.com/0/public/OHLC"
FUT = "https://futures.kraken.com/derivatives/api/v3"
UA = {"User-Agent": "crypto-coach-watcher/1.0"}


# ----------------------------------------------------------------- helpers
def get_json(url, params=None, timeout=20):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def now_ts():
    return int(time.time())


def sma(vals, n):
    if len(vals) < n:
        return None
    return sum(vals[-n:]) / n


def rsi(closes, n=14):
    """Wilder's RSI on the full series; returns the latest value."""
    if len(closes) <= n:
        return None
    gains, losses = 0.0, 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def pct(a, b):
    if a is None or b in (None, 0):
        return None
    return (a / b - 1) * 100


def rnd(x, d=2):
    return None if x is None else round(x, d)


# ----------------------------------------------------------------- data
def candles(pair, interval):
    """Returns list of dicts t,o,h,l,c,v (oldest first, last = current candle)."""
    if os.environ.get("DEMO_OFFLINE"):
        out = fake_candles(pair, interval)
        validate(out, pair, interval)
        return out
    data = get_json(SPOT, {"pair": pair, "interval": interval})
    if data.get("error"):
        raise RuntimeError(f"Kraken {pair}: {data['error']}")
    res = data["result"]
    key = next(k for k in res if k != "last")
    out = [
        {"t": int(r[0]), "o": float(r[1]), "h": float(r[2]), "l": float(r[3]),
         "c": float(r[4]), "v": float(r[6])}
        for r in res[key]
    ]
    validate(out, pair, interval)
    return out


def validate(cs, pair, interval):
    """Refuse bad data instead of publishing wrong signals."""
    if len(cs) < 30:
        raise RuntimeError(f"{pair} {interval}m: only {len(cs)} candles")
    if any(b["t"] <= a["t"] for a, b in zip(cs, cs[1:])):
        raise RuntimeError(f"{pair} {interval}m: candles out of order")
    if any(not (c["l"] > 0 and c["l"] <= min(c["o"], c["c"]) + 1e-12 and c["h"] >= max(c["o"], c["c"]) - 1e-12) for c in cs):
        raise RuntimeError(f"{pair} {interval}m: impossible candle (low/high)")
    max_age = max(2 * interval * 60, 2 * 86400) if interval < 10080 else 15 * 86400
    if now_ts() - cs[-1]["t"] > max_age:
        raise RuntimeError(f"{pair} {interval}m: latest candle is too old")


def futures_info(symbol):
    """Latest open interest + average relative funding of the last 8 periods."""
    if not symbol:
        return None
    if os.environ.get("DEMO_OFFLINE"):
        rnd_ = random.Random(symbol)
        return {"oi": rnd_.uniform(1e6, 5e7), "funding": rnd_.uniform(-0.00002, 0.00008)}
    out = {"oi": None, "funding": None}
    try:
        t = get_json(f"{FUT}/tickers/{symbol}")
        tick = t.get("ticker") or (t.get("tickers") or [{}])[0]
        out["oi"] = tick.get("openInterest")
    except Exception as e:  # noqa: BLE001
        out["oi_error"] = str(e)[:120]
    try:
        h = get_json(f"{FUT}/historical-funding-rates", {"symbol": symbol})
        rates = [r["relativeFundingRate"] for r in h.get("rates", [])[-8:]]
        if rates:
            out["funding"] = sum(rates) / len(rates)
    except Exception as e:  # noqa: BLE001
        out["funding_error"] = str(e)[:120]
    return out


def fake_candles(pair, interval):
    """Deterministic synthetic market for offline tests."""
    r = random.Random(pair + str(interval))
    n = 720
    step = interval * 60
    price = {"ETHUSD": 3000, "SOLUSD": 150, "XRPUSD": 2.0, "QNTUSD": 60}.get(pair, 10)
    t0 = now_ts() - n * step
    out = []
    for i in range(n):
        drift = -0.0012 if i > n * 0.8 else 0.0006
        if pair == "QNTUSD" and i > n - 8 and interval == 1440:
            drift = 0.22  # parabolic pump at the end
        o = price
        price = max(0.01, price * math.exp(drift + r.gauss(0, 0.035)))
        h, l = max(o, price) * (1 + abs(r.gauss(0, 0.01))), min(o, price) * (1 - abs(r.gauss(0, 0.01)))
        out.append({"t": t0 + i * step, "o": o, "h": h, "l": l, "c": price, "v": r.uniform(1e4, 5e4)})
    return out


# ----------------------------------------------------------------- scoring
def score_coin(coin, th, ladder, prev):
    d = candles(coin["kraken"], 1440)
    w = candles(coin["kraken"], 10080)
    h4 = candles(coin["kraken"], 240)
    h1 = candles(coin["kraken"], 60)
    closes = [c["c"] for c in d]
    wcloses = [c["c"] for c in w]
    price = closes[-1]

    ma20, ma50, ma200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
    ma200w = sma(wcloses, 200)
    rsi_d, rsi_w = rsi(closes), rsi(wcloses)
    ch1 = pct(price, closes[-2]) if len(closes) > 1 else None
    ch7 = pct(price, closes[-8]) if len(closes) > 7 else None
    ch30 = pct(price, closes[-31]) if len(closes) > 30 else None
    high90 = max(c["h"] for c in d[-90:])
    ath_window = max(c["h"] for c in d)

    fut = futures_info(coin.get("futures"))

    # --- open-interest history (one sample per ~day, kept in signals.json)
    oi_hist = list((prev or {}).get("oi_history", []))
    if fut and fut.get("oi"):
        if not oi_hist or now_ts() - oi_hist[-1][0] > 20 * 3600:
            oi_hist.append([now_ts(), fut["oi"]])
        oi_hist = oi_hist[-10:]
    oi_growth = None
    if len(oi_hist) >= 2 and fut and fut.get("oi"):
        week_ago = [o for o in oi_hist if now_ts() - o[0] >= 6 * 86400]
        base = (week_ago[-1] if week_ago else oi_hist[0])[1]
        oi_growth = pct(fut["oi"], base)

    # --- top-warning flags the watcher can measure (1-4)
    f_vertical = (ch7 is not None and ch7 >= th["vertical_7d_pct"]) or (ch30 is not None and ch30 >= th["vertical_30d_pct"])
    stretch50, stretch20 = pct(price, ma50), pct(price, ma20)
    f_stretched = (stretch50 is not None and stretch50 >= th["stretched_ma50_pct"]) or (stretch20 is not None and stretch20 >= th["stretched_ma20_pct"])
    f_rsi = (rsi_d is not None and rsi_d >= th["rsi_daily_hot"]) or (rsi_w is not None and rsi_w >= th["rsi_weekly_hot"])
    funding = fut.get("funding") if fut else None
    f_leverage = (funding is not None and funding >= th["funding_hot"]) or (
        oi_growth is not None and oi_growth >= th["oi_growth_7d_pct"] and (ch7 or 0) >= 15)

    flags = {
        "vertical": {"on": bool(f_vertical), "detail": f"7d {fmtp(ch7)}, 30d {fmtp(ch30)}"},
        "stretched": {"on": bool(f_stretched), "detail": f"{fmtp(stretch50)} vs 50-day avg, {fmtp(stretch20)} vs 20-day"},
        "rsi": {"on": bool(f_rsi), "detail": f"daily RSI {rnd(rsi_d, 0)}, weekly RSI {rnd(rsi_w, 0)}"},
        "leverage": {"on": bool(f_leverage),
                     "detail": ("no futures data" if not fut else
                                f"funding {fmtf(funding)}, open interest 7d {fmtp(oi_growth)}")},
    }
    auto_count = sum(1 for f in flags.values() if f["on"])

    # --- buy / bottom signals
    near200w = ma200w is not None and price <= ma200w * (1 + th["near_200w_pct"] / 100)
    cold = rsi_w is not None and rsi_w <= th["rsi_weekly_cold"]
    capit = False
    if len(d) > 35:
        for c_i in range(len(d) - 4, len(d) - 1):  # last 3 completed days
            c = d[c_i]
            avgv = sum(x["v"] for x in d[c_i - 30:c_i]) / 30
            if avgv and c["v"] >= th["capitulation_volume_x"] * avgv and pct(c["c"], c["o"]) <= -th["capitulation_drop_pct"]:
                capit = True
    bottoms = {
        "near_200w": {"on": bool(near200w), "detail": f"200-week avg {money(ma200w)} ({fmtp(pct(price, ma200w))} away)"},
        "rsi_cold": {"on": bool(cold), "detail": f"weekly RSI {rnd(rsi_w, 0)}"},
        "capitulation": {"on": capit, "detail": "big-volume red day in last 3 days" if capit else "none in last 3 days"},
    }

    # --- dip ladder
    anchor = coin.get("anchor")
    p_anchor = (prev or {}).get("ladder", {}).get("anchor")
    if anchor is None:
        anchor = p_anchor or price
    reset = p_anchor is not None and abs(anchor - p_anchor) > 1e-12
    anchor_ts = now_ts() if (reset or not prev) else prev["ladder"].get("anchor_ts", now_ts())
    low = price if (reset or not prev) else min(prev["ladder"].get("low_since", price), price)
    for c in d:
        if c["t"] >= anchor_ts:
            low = min(low, c["l"])
    rungs = []
    for i, r in enumerate(ladder):
        rp = anchor * (1 - r["drop"] / 100)
        rungs.append({"n": i + 1, "drop": r["drop"], "share": r["share"], "price": rp, "hit": low <= rp})

    trail = high90 * (1 - th["trailing_stop_pct"] / 100)

    return {
        "symbol": coin["symbol"], "name": coin.get("name", coin["symbol"]),
        "ws_symbol": coin.get("ws") or f"{coin['symbol']}/USD",
        "_candles": {k: [[c["t"], c["o"], c["h"], c["l"], c["c"], c["v"]] for c in v[-keep:]]
                     for k, v, keep in (("1h", h1, 300), ("4h", h4, 300), ("1d", d, 400), ("1w", w, 260))},
        "price": price, "change_24h": rnd(ch1), "change_7d": rnd(ch7), "change_30d": rnd(ch30),
        "ma20": ma20, "ma50": ma50, "ma200": ma200, "ma200w": ma200w,
        "rsi_daily": rnd(rsi_d, 1), "rsi_weekly": rnd(rsi_w, 1),
        "high_90d": high90, "high_window": ath_window, "drawdown_pct": rnd(pct(price, ath_window)),
        "funding": funding, "open_interest": fut.get("oi") if fut else None, "oi_growth_7d": rnd(oi_growth),
        "flags": flags, "auto_flag_count": auto_count,
        "bottoms": bottoms, "bottom_count": sum(1 for b in bottoms.values() if b["on"]),
        "ladder": {"anchor": anchor, "anchor_ts": anchor_ts, "low_since": low, "rungs": rungs},
        "trailing_stop": trail,
        "oi_history": oi_hist,
        "spark": [round(c, 8) for c in closes[-60:]],
        "alerted": (prev or {}).get("alerted", {}),
    }


def fmtp(x):
    return "n/a" if x is None else f"{x:+.1f}%"


def fmtf(x):
    return "n/a" if x is None else f"{x * 100:.4f}%"


def money(x):
    if x is None:
        return "n/a"
    return f"${x:,.2f}" if x >= 1 else f"${x:.4f}"


# ----------------------------------------------------------------- alerts
def tier(n):
    return 0 if n < 3 else 1 if n == 3 else 2 if n <= 5 else 3


def build_alerts(s, first_run):
    msgs = []
    al = s["alerted"]
    t = tier(s["auto_flag_count"])
    if t > al.get("tier", 0) and not first_run:
        names = [k for k, f in s["flags"].items() if f["on"]]
        msgs.append(f"🔥 {s['symbol']} {s['auto_flag_count']}/4 measured top flags ({', '.join(names)}) at {money(s['price'])}. "
                    f"Check the 3 judgment flags in the app — your rule may say trim.")
    al["tier"] = t
    hit = [r["n"] for r in s["ladder"]["rungs"] if r["hit"]]
    old = set(al.get("rungs", []))
    new = [n for n in hit if n not in old]
    if new and not first_run:
        for n in new:
            r = s["ladder"]["rungs"][n - 1]
            msgs.append(f"🪜 {s['symbol']} ladder rung {n} reached ({money(r['price'])}, −{r['drop']}%). "
                        f"Rule: deploy {r['share']}% of your {s['symbol']} dip reserve. Log it in the app for XP.")
    al["rungs"] = hit
    bc = s["bottom_count"]
    if bc >= 2 and al.get("bottoms", 0) < 2 and not first_run:
        msgs.append(f"🧊 {s['symbol']} {bc}/3 bottom signals on — historically a strong accumulation zone.")
    al["bottoms"] = bc
    if s["price"] <= s["trailing_stop"] and not al.get("trail") and not first_run:
        msgs.append(f"🛑 {s['symbol']} below trailing stop {money(s['trailing_stop'])}. If you trimmed this cycle, your rule says sell the moonbag.")
    al["trail"] = s["price"] <= s["trailing_stop"]
    return msgs


def telegram(text):
    tok, chat = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if not tok or not chat:
        print("[telegram not configured]\n" + text)
        return
    body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{tok}/sendMessage", data=body, headers=UA)
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:  # noqa: BLE001
        print("telegram failed:", e, file=sys.stderr)


# ----------------------------------------------------------------- main
def load_prev(path_or_url):
    if not path_or_url:
        return None
    try:
        if path_or_url.startswith("http"):
            return get_json(path_or_url + ("&" if "?" in path_or_url else "?") + f"t={now_ts()}")
        with open(path_or_url) as f:
            return json.load(f)
    except urllib.error.HTTPError as e:
        if e.code == 404:  # genuinely the first run: nothing published yet
            print("no previous signals (first run)")
            return None
        sys.exit(f"Couldn't read previous signals ({e}). Skipping this run so the ladder memory isn't reset.")
    except FileNotFoundError:
        return None
    except Exception as e:  # noqa: BLE001
        sys.exit(f"Couldn't read previous signals ({e}). Skipping this run so the ladder memory isn't reset.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="coins.json")
    ap.add_argument("--out", default="site/signals.json")
    ap.add_argument("--prev", default=os.environ.get("PREV_URL", ""))
    a = ap.parse_args()

    with open(a.config) as f:
        cfg = json.load(f)
    prev = load_prev(a.prev)
    prev_coins = {c["symbol"]: c for c in (prev or {}).get("coins", [])}
    first_run = prev is None

    out, alerts = [], []
    for coin in cfg["coins"]:
        try:
            s = score_coin(coin, cfg["thresholds"], cfg["ladder"], prev_coins.get(coin["symbol"]))
            alerts += build_alerts(s, first_run or coin["symbol"] not in prev_coins)
            cdir = os.path.join(os.path.dirname(a.out) or ".", "candles")
            os.makedirs(cdir, exist_ok=True)
            with open(os.path.join(cdir, f"{coin['symbol']}.json"), "w") as f:
                json.dump(s.pop("_candles"), f, separators=(",", ":"))
            out.append(s)
        except Exception as e:  # noqa: BLE001
            print(f"{coin['symbol']} failed: {e}", file=sys.stderr)
            keep = prev_coins.get(coin["symbol"])
            if keep:
                keep["stale"] = True
                out.append(keep)
        time.sleep(1.1)  # stay well inside Kraken's public rate limit

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "Kraken public API" if not os.environ.get("DEMO_OFFLINE") else "synthetic test data",
        "thresholds": cfg["thresholds"],
        "ladder": cfg["ladder"],
        "coins": out,
    }
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"wrote {a.out}: {len(out)} coins")

    if first_run and out:
        lines = [f"✅ Crypto Coach watcher is live. Tracking {', '.join(c['symbol'] for c in out)}."]
        for c in out:
            lines.append(f"{c['symbol']} {money(c['price'])} · top flags {c['auto_flag_count']}/4 · bottom signals {c['bottom_count']}/3")
        telegram("\n".join(lines))
    elif alerts:
        telegram("\n\n".join(alerts))


if __name__ == "__main__":
    main()
