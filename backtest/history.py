"""Long daily price history from free public sources (no keys).

Kraken only gives ~720 daily candles, so the stress test uses:
  Coinbase Exchange  BTC-USD, ETH-USD, SOL-USD, QNT-USD   (300 candles per request)
  Bitstamp           xrpusd                               (1000 candles per request;
                     Coinbase has a hole in XRP while it was delisted 2021-2023)

Every series is checked: sorted, one candle per day, sane OHLC. If a source has a gap of
more than GAP_DAYS, only the latest unbroken stretch is used (and that's reported).
Candles: {"t": day start (unix, UTC), "o", "h", "l", "c", "v"}.
"""
import json
import time
import urllib.parse
import urllib.request

DAY = 86400
GAP_DAYS = 5
UA = {"User-Agent": "crypto-coach-stress/1.0"}
SOURCES = {
    "BTC": ("coinbase", "BTC-USD"),
    "ETH": ("coinbase", "ETH-USD"),
    "SOL": ("coinbase", "SOL-USD"),
    "QNT": ("coinbase", "QNT-USD"),
    "XRP": ("bitstamp", "xrpusd"),
    "LTC": ("coinbase", "LTC-USD"),
    "ETC": ("coinbase", "ETC-USD"),
}
EARLIEST = 1451606400   # 2016-01-01


def _get(url, params):
    req = urllib.request.Request(url + "?" + urllib.parse.urlencode(params), headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


def parse_coinbase(rows):
    """Coinbase rows: [time, low, high, open, close, volume], newest first."""
    return [{"t": int(r[0]), "o": float(r[3]), "h": float(r[2]), "l": float(r[1]), "c": float(r[4]), "v": float(r[5])}
            for r in rows]


def parse_bitstamp(doc):
    rows = (doc.get("data") or {}).get("ohlc") or []
    return [{"t": int(r["timestamp"]), "o": float(r["open"]), "h": float(r["high"]), "l": float(r["low"]),
             "c": float(r["close"]), "v": float(r["volume"])} for r in rows]


def fetch_coinbase(product, now=None, get=_get, pause=0.35):
    now = int(now or time.time())
    out, end = [], now - now % DAY
    while end > EARLIEST:
        start = max(EARLIEST, end - 299 * DAY)
        rows = get(f"https://api.exchange.coinbase.com/products/{product}/candles",
                   {"granularity": DAY, "start": iso(start), "end": iso(end)})
        got = parse_coinbase(rows)
        if not got and out:
            break                       # reached the listing date
        out += got
        end = start - DAY
        time.sleep(pause)
    return out


def fetch_bitstamp(pair, now=None, get=_get, pause=0.35):
    now = int(now or time.time())
    out, start = [], EARLIEST
    while start < now:
        got = parse_bitstamp(get(f"https://www.bitstamp.net/api/v2/ohlc/{pair}/",
                                 {"step": DAY, "limit": 1000, "start": start}))
        if not got:
            break
        out += got
        nxt = max(c["t"] for c in got) + DAY
        if nxt <= start:
            break
        start = nxt
        time.sleep(pause)
    return out


def clean(candles, now=None):
    """Sort, de-duplicate, drop today's unfinished candle and bad rows, keep the latest unbroken stretch."""
    now = int(now or time.time())
    by_t = {}
    for c in candles:
        t = c["t"] - c["t"] % DAY
        if t + DAY > now:
            continue                    # unfinished day
        if min(c["o"], c["h"], c["l"], c["c"]) <= 0:
            continue
        if c["l"] > min(c["o"], c["c"]) * 1.0000001 or c["h"] < max(c["o"], c["c"]) * 0.9999999:
            continue
        by_t[t] = dict(c, t=t)
    cs = [by_t[t] for t in sorted(by_t)]
    start, gaps = 0, []
    for i in range(1, len(cs)):
        d = (cs[i]["t"] - cs[i - 1]["t"]) // DAY
        if d > GAP_DAYS:
            gaps.append((cs[i - 1]["t"], cs[i]["t"]))
            start = i
    cs = cs[start:]
    filled = []                         # fill short gaps with flat candles at the last close
    for c in cs:
        while filled and c["t"] - filled[-1]["t"] > DAY:
            p = filled[-1]["c"]
            filled.append({"t": filled[-1]["t"] + DAY, "o": p, "h": p, "l": p, "c": p, "v": 0.0})
        filled.append(c)
    return filled, gaps


def load(symbol, now=None):
    src, code = SOURCES[symbol]
    raw = fetch_coinbase(code, now) if src == "coinbase" else fetch_bitstamp(code, now)
    cs, gaps = clean(raw, now)
    if len(cs) < 400:
        raise RuntimeError(f"{symbol}: only {len(cs)} usable days from {src}")
    return cs, {"source": f"{src} {code}", "from": time.strftime("%Y-%m-%d", time.gmtime(cs[0]["t"])),
                "to": time.strftime("%Y-%m-%d", time.gmtime(cs[-1]["t"])), "days": len(cs),
                "gaps_skipped": len(gaps)}
