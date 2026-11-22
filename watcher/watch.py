#!/usr/bin/env python3
"""Crypto Coach watcher.

Pulls free public data from Kraken, scores the top-warning flags and the
buy signals for every coin in coins.json, tracks the dip ladder, writes
site/signals.json for the app, and sends a Telegram alert only when
something changes. Standard library only - no installs needed.

Usage:  python watcher/watch.py [--config coins.json] [--out site/signals.json]
Env:    PREV_URL            URL of the last published signals.json (for memory between runs)
        NO_NEWS=1           skip the news feeds (for testing)
        alert channels      see watcher/notify.py (ntfy, Telegram, WhatsApp)
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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import engine  # noqa: E402
import news  # noqa: E402
import notify  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(HERE, "..", "VERSION")) as _f:
        VERSION = _f.read().strip()
except OSError:
    VERSION = "unknown"

SPOT = "https://api.kraken.com/0/public/OHLC"
FUT = "https://futures.kraken.com/derivatives/api/v3"
UA = {"User-Agent": "crypto-coach-watcher/1.0"}
FEED_UA = {"User-Agent": "Mozilla/5.0 (compatible; crypto-coach-watcher/1.0)",
           "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml, */*"}
TRENDING = "https://api.coingecko.com/api/v3/search/trending"


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
    if ag == 0 and al == 0:
        return 50.0  # no movement at all: neutral (0/0), not overbought
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


def funding_per_8h(rates):
    """rates: [(unix_ts, relative_rate), ...] oldest first.
    Average of the last 8 periods, converted to an 8-hour rate using the actual
    spacing between periods (Kraken has used 1h and 4h periods)."""
    rates = sorted(rates)[-9:]
    if len(rates) < 2:
        return None
    gaps = sorted((b[0] - a[0]) / 3600 for a, b in zip(rates, rates[1:]))
    period_h = gaps[len(gaps) // 2]  # median spacing
    if period_h <= 0:
        return None
    last8 = [r for _, r in rates[-8:]]
    return sum(last8) / len(last8) * (8 / period_h)


def futures_info(symbol):
    """Latest open interest + funding rate expressed per 8 hours."""
    if not symbol:
        return None
    if os.environ.get("DEMO_OFFLINE"):
        rnd_ = random.Random(symbol)
        return {"oi": rnd_.uniform(1e6, 5e7), "funding": rnd_.uniform(-0.0002, 0.0003)}
    out = {"oi": None, "funding": None}
    try:
        t = get_json(f"{FUT}/tickers/{symbol}")
        tick = t.get("ticker") or (t.get("tickers") or [{}])[0]
        out["oi"] = tick.get("openInterest")
    except Exception as e:  # noqa: BLE001
        out["oi_error"] = str(e)[:120]
    try:
        h = get_json(f"{FUT}/historical-funding-rates", {"symbol": symbol})
        rates = [(news.parse_date(r["timestamp"]), float(r["relativeFundingRate"])) for r in h.get("rates", [])]
        out["funding"] = funding_per_8h([r for r in rates if r[0]])
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
def score_coin(coin, th, ladder, prev, rules=None):
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
    oi_growth = None  # only once there is a sample at least 6 days old
    if fut and fut.get("oi"):
        week_ago = [o for o in oi_hist if now_ts() - o[0] >= 6 * 86400]
        if week_ago:
            oi_growth = pct(fut["oi"], week_ago[-1][1])

    # --- top-warning flags the watcher can measure (1-4)
    f_vertical = (ch7 is not None and ch7 >= th["vertical_7d_pct"]) or (ch30 is not None and ch30 >= th["vertical_30d_pct"])
    stretch50, stretch20 = pct(price, ma50), pct(price, ma20)
    f_stretched = (stretch50 is not None and stretch50 >= th["stretched_ma50_pct"]) or (stretch20 is not None and stretch20 >= th["stretched_ma20_pct"])
    f_rsi = (rsi_d is not None and rsi_d >= th["rsi_daily_hot"]) or (rsi_w is not None and rsi_w >= th["rsi_weekly_hot"])
    funding = fut.get("funding") if fut else None
    f_leverage = (funding is not None and funding >= th["funding_hot_8h"]) or (
        oi_growth is not None and oi_growth >= th["oi_growth_7d_pct"] and (ch7 or 0) >= 15)

    flags = {
        "vertical": {"on": bool(f_vertical), "detail": f"7d {fmtp(ch7)}, 30d {fmtp(ch30)}"},
        "stretched": {"on": bool(f_stretched), "detail": f"{fmtp(stretch50)} vs 50-day avg, {fmtp(stretch20)} vs 20-day"},
        "rsi": {"on": bool(f_rsi), "detail": f"daily RSI {rnd(rsi_d, 0)}, weekly RSI {rnd(rsi_w, 0)}"},
        "leverage": {"on": bool(f_leverage),
                     "detail": ("no futures data" if not fut else
                                f"funding {fmtf(funding)}/8h, open interest 7d "
                                + (fmtp(oi_growth) if oi_growth is not None else "n/a (needs 6+ days of samples)"))},
    }

    # --- buy / bottom signals
    # Each one mirrors a top flag, pointing the other way.
    b_freefall = (ch7 is not None and ch7 <= -th["freefall_7d_pct"]) or (ch30 is not None and ch30 <= -th["freefall_30d_pct"])
    b_below = (stretch50 is not None and stretch50 <= -th["stretched_down_ma50_pct"]) or (
        stretch20 is not None and stretch20 <= -th["stretched_down_ma20_pct"])
    b_oversold = (rsi_d is not None and rsi_d <= th["rsi_daily_cold"]) or (rsi_w is not None and rsi_w <= th["rsi_weekly_cold"])
    b_shorts = funding is not None and funding <= th["funding_cold_8h"]
    near200w = ma200w is not None and price <= ma200w * (1 + th["near_200w_pct"] / 100)
    capit = False
    if len(d) > 35:
        for c_i in range(len(d) - 4, len(d) - 1):  # last 3 completed days
            c = d[c_i]
            avgv = sum(x["v"] for x in d[c_i - 30:c_i]) / 30
            if avgv and c["v"] >= th["capitulation_volume_x"] * avgv and pct(c["c"], c["o"]) <= -th["capitulation_drop_pct"]:
                capit = True
    bottoms = {
        "freefall": {"on": bool(b_freefall), "detail": f"7d {fmtp(ch7)}, 30d {fmtp(ch30)}"},
        "stretched_down": {"on": bool(b_below), "detail": f"{fmtp(stretch50)} vs 50-day avg, {fmtp(stretch20)} vs 20-day"},
        "oversold": {"on": bool(b_oversold), "detail": f"daily RSI {rnd(rsi_d, 0)}, weekly RSI {rnd(rsi_w, 0)}"},
        "shorts_crowded": {"on": bool(b_shorts), "detail": "no futures data" if not fut else f"funding {fmtf(funding)}/8h"},
        "capitulation": {"on": capit, "detail": "big-volume red day in last 3 days" if capit else "none in last 3 days"},
        "near_200w": {"on": bool(near200w), "detail": f"200-week avg {money(ma200w)} ({fmtp(pct(price, ma200w))} away)"},
    }

    # --- v4 orders engine (take-profits, buy-backs, dip ladder)
    rules = rules or engine.DEFAULT_RULES
    cost, anchor_cfg = coin.get("cost"), coin.get("anchor")
    est = (prev or {}).get("engine")
    if not est:
        est = engine.new_state(cost or price, anchor_cfg or price, rules)
        est["last_ts"] = now_ts()
        new_events = []
    else:
        engine.sync_config(est, cost, anchor_cfg, rules)
        since = est.get("last_ts", now_ts())
        src = h1 if h1 and since >= h1[0]["t"] else d          # hourly detail, or daily after a long gap
        period = [c for c in src if c["t"] + (3600 if src is h1 else 86400) > since]
        hi = max([c["h"] for c in period] + [price])
        lo = min([c["l"] for c in period] + [price])
        new_events = engine.step(est, hi, lo, now_ts(), rules)
        est["last_ts"] = now_ts()

    trail = high90 * (1 - th["trailing_stop_pct"] / 100)  # reference only; the app's stop uses the peak since your trim

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
        "flags": flags, "auto_flag_count": None,  # set after the news step
        "bottoms": bottoms, "bottom_count": sum(1 for b in bottoms.values() if b["on"]),
        "engine": est, "orders": engine.open_orders(est, rules), "new_events": new_events,
        "trailing_stop": trail,
        "oi_history": oi_hist,
        "spark": [round(c, 8) for c in closes[-60:]],
        "alerted": (prev or {}).get("alerted", {}),
    }


def fmtp(x):
    return "n/a" if x is None else f"{x:+.1f}%"


def fmtf(x):
    return "n/a" if x is None else f"{x * 100:.4f}%"


FLAG_NAMES = {"vertical": "vertical move", "stretched": "stretched", "rsi": "extreme RSI",
              "leverage": "leverage", "news": "news priced in", "fomo": "social FOMO"}


def money(x):
    if x is None:
        return "n/a"
    return f"${x:,.2f}" if x >= 1 else f"${x:.4f}"


# ----------------------------------------------------------------- alerts
def tier(n):
    return 0 if n < 3 else 1 if n == 3 else 2 if n <= 5 else 3


def build_alerts(s, first_run):
    """Returns [(text, priority)] for this coin. Priority 4 = act now, 3 = heads-up."""
    out = []
    sym = s["symbol"]
    if not first_run:
        for e in s.get("new_events", []):
            if e["kind"] == "take_profit":
                out.append((f"🔴 JUMP OUT · {sym} reached {money(e['price'])}, your +{e['pct']}% take-profit. "
                            f"Sell {e['share']}% of your trade bucket (the app shows how many coins). "
                            f"Buy-back orders are now set below it.", 4))
            elif e["kind"] == "buy_back":
                out.append((f"🟢 JUMP IN · {sym} fell to {money(e['price'])}: buy back with {e['share']:.0f}% "
                            f"of the money from that sale. Your next take-profits restart from here.", 4))
            elif e["kind"] == "dip_buy":
                out.append((f"🟢 JUMP IN · {sym} dropped to {money(e['price'])} (dip rung {e['rung']}): "
                            f"spend {e['share']}% of your {sym} dip reserve.", 4))
    al = s["alerted"]
    t = tier(s["auto_flag_count"])
    if t > al.get("tier", 0) and not first_run:
        names = [FLAG_NAMES.get(k, k) for k, f in s["flags"].items() if f["on"]]
        out.append((f"🟡 HEADS UP · {sym} looks overheated at {money(s['price'])} ({s['auto_flag_count']}/6: "
                    f"{', '.join(names)}). Make sure your take-profit orders are set.", 3))
    al["tier"] = t
    bt = tier(s["bottom_count"])
    if bt > al.get("btier", 0) and not first_run:
        names = [k.replace("_", " ") for k, b in s["bottoms"].items() if b["on"]]
        out.append((f"🟡 HEADS UP · {sym} looks washed out at {money(s['price'])} ({s['bottom_count']}/6: "
                    f"{', '.join(names)}). Make sure your buy orders are set.", 3))
    al["btier"] = bt
    return out


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


def fetch_news(sources):
    """Returns ([(source, items)], [status per source])."""
    fetched, status = [], []
    for src in sources:
        try:
            if os.environ.get("DEMO_OFFLINE"):
                data = fake_feed(src)
            else:
                req = urllib.request.Request(src["url"], headers=FEED_UA)
                with urllib.request.urlopen(req, timeout=20) as r:
                    data = r.read()
            items = news.parse_feed(data)
            fetched.append((src, items))
            status.append({"name": src["name"], "ok": True, "items": len(items)})
        except Exception as e:  # noqa: BLE001
            status.append({"name": src["name"], "ok": False, "error": str(e)[:120]})
    return fetched, status


def fetch_trending(prev):
    """CoinGecko's top trending searches (last 24h). Falls back to the last
    good list if it is under 6 hours old."""
    try:
        if os.environ.get("DEMO_OFFLINE"):
            ids = ["quant-network", "zcash", "bonk"]
        else:
            data = get_json(TRENDING)
            ids = [c["item"]["id"] for c in data.get("coins", [])]
        return {"ids": ids, "ts": now_ts(), "ok": True}
    except Exception as e:  # noqa: BLE001
        if prev and prev.get("ok") is not False and now_ts() - prev.get("ts", 0) < 6 * 3600:
            return dict(prev, note="using last list, refresh failed")
        return {"ids": [], "ts": now_ts(), "ok": False, "error": str(e)[:120]}


def fake_feed(src):
    """Offline test feed: a QNT hype burst plus ordinary items."""
    t = now_ts()
    titles = ["Quant soars 40% after bank partnership", "QNT price prediction: could hit $300?",
              "Quant partners with major clearing house", "Fed holds rates steady as inflation cools",
              "Ethereum developers schedule mainnet upgrade", "Solana outage resolved after 2 hours",
              "XRP ETF filing gets SEC response"]
    items = "".join(
        f"<item><title>{x}</title><link>https://example.com/{src['name']}/{i}</link>"
        f"<pubDate>{datetime.fromtimestamp(t - i * 3600, timezone.utc).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate>"
        f"<description>{x}</description></item>"
        for i, x in enumerate(titles))
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items}</channel></rss>'.encode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default="coins.json")
    ap.add_argument("--out", default="site/signals.json")
    ap.add_argument("--prev", default=os.environ.get("PREV_URL", ""))
    a = ap.parse_args()

    with open(a.config) as f:
        cfg = json.load(f)
    th = cfg["thresholds"]
    prev = load_prev(a.prev)
    prev_coins = {c["symbol"]: c for c in (prev or {}).get("coins", [])}
    first_run = prev is None

    # 1) market data + flags 1-4 + bottoms + ladder for each coin
    out, new_coin = [], set()
    for coin in cfg["coins"]:
        try:
            s = score_coin(coin, th, cfg["ladder"], prev_coins.get(coin["symbol"]), engine.rules_from(cfg.get("rules")))
            cdir = os.path.join(os.path.dirname(a.out) or ".", "candles")
            os.makedirs(cdir, exist_ok=True)
            with open(os.path.join(cdir, f"{coin['symbol']}.json"), "w") as f:
                json.dump(s.pop("_candles"), f, separators=(",", ":"))
            out.append(s)
            if coin["symbol"] not in prev_coins:
                new_coin.add(coin["symbol"])
        except Exception as e:  # noqa: BLE001
            print(f"{coin['symbol']} failed: {e}", file=sys.stderr)
            keep = prev_coins.get(coin["symbol"])
            if keep:
                keep["stale"] = True
                out.append(keep)
        time.sleep(1.1)  # stay well inside Kraken's public rate limit

    # 2) news + trending -> flags 5-6 (news priced in, social FOMO)
    now = now_ts()
    prev_news = (prev or {}).get("news") or {}
    if os.environ.get("NO_NEWS"):
        fetched, status = [], [{"name": "news disabled", "ok": False, "error": "NO_NEWS set"}]
    else:
        fetched, status = fetch_news(cfg.get("news_sources", []))
    news_ok = any(x["ok"] for x in status)
    trending = fetch_trending(prev_news.get("trending"))
    state = news.update_state({k: prev_news.get(k) for k in ("items", "seen", "daily", "since") if prev_news.get(k) is not None},
                              fetched, cfg["coins"], now)
    cg = {c["symbol"]: c.get("coingecko") for c in cfg["coins"]}
    for s in out:
        if s.get("stale"):
            continue
        ids = trending.get("ids", [])
        rank = (ids.index(cg[s["symbol"]]) + 1) if cg.get(s["symbol"]) in ids else None
        m = news.coin_metrics(state, s["symbol"], now, th, rank)
        s["news"] = m
        s["flags"].update(news.news_flags(m, s.get("change_7d"), th, news_ok, trending.get("ok", False)))
        s["auto_flag_count"] = sum(1 for f in s["flags"].values() if f["on"])

    alerts = []
    for s in out:
        if not s.get("stale"):
            alerts += build_alerts(s, first_run or s["symbol"] in new_coin)

    doc = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "version": VERSION,
        "source": "Kraken public API" if not os.environ.get("DEMO_OFFLINE") else "synthetic test data",
        "thresholds": th,
        "rules": engine.rules_from(cfg.get("rules")),
        "coins": out,
        "news": dict(state, sources=status, trending=trending),
    }
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"wrote {a.out}: {len(out)} coins, {len(state['items'])} news items, "
          f"{sum(1 for x in status if x['ok'])}/{len(status)} feeds ok")

    if first_run and out:
        lines = [f"✅ Crypto Coach watcher is live. Tracking {', '.join(c['symbol'] for c in out)}."]
        for c in out:
            lines.append(f"{c['symbol']} {money(c['price'])} · top flags {c['auto_flag_count']}/6 · bottom signals {c['bottom_count']}/3")
        notify.send_all([("\n".join(lines), 3)])
    elif alerts:
        notify.send_all(alerts)


if __name__ == "__main__":
    main()
