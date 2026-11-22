"""Crypto Coach maths checks. Runs before every watcher run; if anything fails,
nothing is published. Plain Python, no installs:  python3 tests/test_watch.py

Three layers:
  1. reference values   - known published answers (RSI) and hand-worked examples
  2. cross-checks       - a second, independent implementation compared on random data
  3. scenarios          - every flag, signal, ladder and alert tested at its exact boundary
"""
import json
import os
import random
import re
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "watcher"))
import news  # noqa: E402
import watch  # noqa: E402

DAY = 86400


def close(a, b, tol):
    assert a is not None and abs(a - b) <= tol, f"{a} != {b} (tol {tol})"


def raises(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001
        return
    raise AssertionError("expected an error")


# =================================================================== 1. reference values
CLOSES = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84, 46.08, 45.89,
          46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41, 46.22, 45.64]


def test_rsi_reference():
    # StockCharts' published 14-day example (their sheet rounds averages, so allow 0.1)
    ref = [70.53, 66.32, 66.55, 69.41, 66.36, 57.97]
    for k, want in zip(range(15, 21), ref):
        close(watch.rsi(CLOSES[:k]), want, 0.1)


def test_rsi_hand_worked():
    # 15 closes: 7 up-moves of +1, 7 down-moves of -0.5 -> avg gain 0.5, avg loss 0.25, RS 2, RSI 66.667
    closes = [10.0]
    for i in range(14):
        closes.append(closes[-1] + (1 if i % 2 == 0 else -0.5))
    close(watch.rsi(closes), 100 - 100 / 3, 1e-9)


def test_rsi_edges():
    assert watch.rsi([1, 2, 3]) is None
    assert watch.rsi([5.0] * 30) == 50.0                    # no movement at all: neutral, not 100
    assert watch.rsi(list(range(1, 30))) == 100.0          # only gains
    close(watch.rsi(list(range(30, 1, -1))), 0.0, 1e-12)    # only losses


def test_sma_pct_hand_worked():
    assert watch.sma([1, 2, 3, 4], 2) == 3.5
    assert watch.sma([1], 2) is None
    close(watch.pct(110, 100), 10.0, 1e-9)
    close(watch.pct(75, 100), -25.0, 1e-9)
    assert watch.pct(1, 0) is None and watch.pct(None, 1) is None


def test_tiers():
    assert [watch.tier(n) for n in range(7)] == [0, 0, 0, 1, 2, 2, 3]


def test_funding_per_8h():
    t0 = 1_700_000_000
    hourly = [(t0 + i * 3600, 0.0001) for i in range(12)]
    close(watch.funding_per_8h(hourly), 0.0008, 1e-15)          # 8 x 0.0001
    four_h = [(t0 + i * 4 * 3600, 0.0004) for i in range(12)]
    close(watch.funding_per_8h(four_h), 0.0008, 1e-15)          # 2 x 0.0004
    mixed = [(t0 + i * 3600, r) for i, r in enumerate([9, 9, 1, 2, 3, 4, 5, 6, 7, 8])]
    random.Random(1).shuffle(mixed)                             # order must not matter
    close(watch.funding_per_8h(mixed), statistics.fmean([1, 2, 3, 4, 5, 6, 7, 8]) * 8, 1e-12)
    assert watch.funding_per_8h([(t0, 0.1)]) is None


# =================================================================== 2. cross-checks
def rsi_independent(closes, n=14):
    """Second implementation, written differently on purpose."""
    ch = [b - a for a, b in zip(closes, closes[1:])]
    if len(ch) < n:
        return None
    up = [c if c > 0 else 0.0 for c in ch]
    dn = [-c if c < 0 else 0.0 for c in ch]
    ag = statistics.fmean(up[:n])
    al = statistics.fmean(dn[:n])
    for u, d in zip(up[n:], dn[n:]):
        ag += (u - ag) / n
        al += (d - al) / n
    if ag == 0 and al == 0:
        return 50.0
    return 100.0 if al == 0 else 100 * ag / (ag + al)


def test_rsi_cross_check_random():
    rnd = random.Random(42)
    for _ in range(400):
        n = rnd.randint(15, 400)
        p = [rnd.uniform(0.5, 500)]
        for _ in range(n - 1):
            p.append(max(0.0001, p[-1] * (1 + rnd.gauss(0, 0.05))))
        a, b = watch.rsi(p), rsi_independent(p)
        assert abs(a - b) < 1e-8, (a, b)


def test_sma_cross_check_random():
    rnd = random.Random(7)
    for _ in range(300):
        v = [rnd.uniform(0, 1000) for _ in range(rnd.randint(1, 300))]
        k = rnd.randint(1, len(v))
        close(watch.sma(v, k), statistics.fmean(v[-k:]), 1e-7)


# =================================================================== 3. scenarios
NOW = int(time.time())


def candle_series(closes, interval_min, vols=None, opens=None, lows=None, highs=None):
    step = interval_min * 60
    t_last = NOW - (NOW % step)
    out = []
    for i, c in enumerate(closes):
        o = opens[i] if opens else (closes[i - 1] if i else c)
        lo = lows[i] if lows else min(o, c) * 0.999
        hi = highs[i] if highs else max(o, c) * 1.001
        out.append({"t": t_last - (len(closes) - 1 - i) * step, "o": o, "h": hi, "l": lo, "c": c,
                    "v": vols[i] if vols else 1000.0})
    return out


TH = json.load(open(os.path.join(HERE, "..", "coins.json")))["thresholds"]
LADDER = [{"drop": 10, "share": 25}, {"drop": 20, "share": 25}, {"drop": 30, "share": 25}, {"drop": 40, "share": 25}]


def run_coin(daily, weekly=None, fut=None, prev=None, anchor=None):
    weekly = weekly or candle_series([100.0] * 60, 10080)
    orig = (watch.candles, watch.futures_info)
    series = {1440: daily, 10080: weekly, 240: candle_series([100.0] * 40, 240), 60: candle_series([100.0] * 40, 60)}
    watch.candles = lambda pair, interval: series[interval]
    watch.futures_info = lambda sym: fut
    try:
        coin = {"symbol": "TST", "kraken": "TSTUSD", "futures": "PF_TSTUSD", "anchor": anchor}
        return watch.score_coin(coin, TH, LADDER, prev)
    finally:
        watch.candles, watch.futures_info = orig


def flat_then(last_closes, base=100.0, n=120):
    return [base] * (n - len(last_closes)) + list(last_closes)


def test_change_windows():
    closes = flat_then([200.0], base=100.0)
    closes[-8] = 50.0        # 7 days before today
    closes[-31] = 25.0       # 30 days before today
    closes[-2] = 160.0       # yesterday
    s = run_coin(candle_series(closes, 1440))
    close(s["change_7d"], 300.0, 1e-9)
    close(s["change_30d"], 700.0, 1e-9)
    close(s["change_24h"], 25.0, 1e-9)


def test_flag_vertical_boundary():
    on = run_coin(candle_series(flat_then([160.0]), 1440))     # exactly +60% in 7d
    off = run_coin(candle_series(flat_then([159.9]), 1440))
    assert on["flags"]["vertical"]["on"] and not off["flags"]["vertical"]["on"]


def test_flag_stretched_boundary():
    # 30 days at 50, 19 days at 120, today P.  50-day rule: P >= 1.5*(1500+2280+P)/50  <=>  P >= 5670/48.5 = 116.907...
    # (the 20-day average is ~120, so the 20-day rule stays off and only the 50-day rule is tested)
    for p, want in ((116.908, True), (116.906, False)):
        closes = [100.0] * 70 + [50.0] * 30 + [120.0] * 19 + [p]
        s = run_coin(candle_series(closes, 1440))
        close(s["ma50"], (1500 + 2280 + p) / 50, 1e-9)
        assert s["flags"]["stretched"]["on"] == want, (p, s["flags"]["stretched"])
    # 20-day rule on its own: 19 days at 100, today P -> P >= 1.35*(1900+P)/20  <=>  P >= 2565/18.65 = 137.53...
    for p, want in ((137.54, True), (137.52, False)):
        closes = [400.0] * 70 + [100.0] * 49 + [p]   # 50-day avg ~101, so +35..37% stays under the 50% rule
        s = run_coin(candle_series(closes, 1440))
        assert s["flags"]["stretched"]["on"] == want, (p, s["flags"]["stretched"])


def test_flag_rsi():
    rising = [100 * 1.01 ** i for i in range(120)]
    s = run_coin(candle_series(rising, 1440))
    assert s["rsi_daily"] == 100.0 and s["flags"]["rsi"]["on"]
    calm = [100 + (1 if i % 2 else -1) for i in range(120)]
    assert not run_coin(candle_series(calm, 1440))["flags"]["rsi"]["on"]


def test_flag_leverage_funding_boundary():
    d = candle_series([100.0] * 120, 1440)
    assert run_coin(d, fut={"oi": None, "funding": 0.0005})["flags"]["leverage"]["on"]
    assert not run_coin(d, fut={"oi": None, "funding": 0.000499})["flags"]["leverage"]["on"]
    assert not run_coin(d, fut=None)["flags"]["leverage"]["on"]


def test_flag_leverage_open_interest_needs_week_old_sample():
    d = candle_series(flat_then([116.0]), 1440)   # +16% in 7 days
    prev_young = {"oi_history": [[NOW - 2 * DAY, 100.0]]}
    s = run_coin(d, fut={"oi": 200.0, "funding": 0.0}, prev=prev_young)
    assert s["oi_growth_7d"] is None and not s["flags"]["leverage"]["on"]
    prev_old = {"oi_history": [[NOW - 7 * DAY, 100.0], [NOW - 2 * DAY, 120.0]]}
    s = run_coin(d, fut={"oi": 130.0, "funding": 0.0}, prev=prev_old)
    close(s["oi_growth_7d"], 30.0, 1e-9)
    assert s["flags"]["leverage"]["on"]
    s = run_coin(d, fut={"oi": 129.0, "funding": 0.0}, prev=prev_old)
    assert not s["flags"]["leverage"]["on"]


def test_bottom_near_200w_boundary():
    weekly = candle_series([100.0] * 260, 10080)
    for p, want in ((110.0, True), (110.01, False), (60.0, True)):
        s = run_coin(candle_series(flat_then([p]), 1440), weekly=weekly)
        close(s["ma200w"], 100.0, 1e-9)
        assert s["bottoms"]["near_200w"]["on"] == want, p


def test_bottom_oversold():
    falling = [100 * 0.98 ** i for i in range(80)]
    s = run_coin(candle_series([100.0] * 120, 1440), weekly=candle_series(falling, 10080))
    close(s["rsi_weekly"], 0.0, 1e-9)
    assert s["bottoms"]["oversold"]["on"]                              # weekly <= 30
    daily = [100 * 0.99 ** i for i in range(120)]
    s = run_coin(candle_series(daily, 1440))
    assert s["rsi_daily"] == 0.0 and s["bottoms"]["oversold"]["on"]    # daily <= 25
    calm = [100 + (1 if i % 2 else -1) for i in range(120)]
    assert not run_coin(candle_series(calm, 1440))["bottoms"]["oversold"]["on"]


def test_bottom_freefall_boundary():
    on = run_coin(candle_series(flat_then([75.0]), 1440))      # exactly -25% in 7d
    off = run_coin(candle_series(flat_then([75.01]), 1440))
    assert on["bottoms"]["freefall"]["on"] and not off["bottoms"]["freefall"]["on"]
    closes = [100.0] * 120
    closes[-31] = 200.0                                        # -50% over 30 days, flat over 7
    s = run_coin(candle_series(closes, 1440))
    assert s["bottoms"]["freefall"]["on"] and abs(s["change_7d"]) < 1e-9


def test_bottom_stretched_down_boundary():
    # 30 days at 200, 19 days at 80, today P. 50-day rule: P <= 0.75*(6000+1520+P)/50  <=>  P <= 5640/49.25 = 114.52...
    # (20-day average ~80, so P well above it keeps the 20-day rule off)
    for p, want in ((114.51, True), (114.53, False)):
        closes = [100.0] * 70 + [200.0] * 30 + [80.0] * 19 + [p]
        s = run_coin(candle_series(closes, 1440))
        assert s["bottoms"]["stretched_down"]["on"] == want, (p, s["bottoms"]["stretched_down"])
    # 20-day rule alone: 19 days at 100, today P <= 0.8*(1900+P)/20  <=>  P <= 1520/19.2 = 79.1666...
    for p, want in ((79.16, True), (79.17, False)):
        closes = [30.0] * 70 + [100.0] * 49 + [p]                # 50-day avg ~99.6 -> about -20.5%, under the 25% rule
        s = run_coin(candle_series(closes, 1440))
        assert s["bottoms"]["stretched_down"]["on"] == want, (p, s["bottoms"]["stretched_down"])


def test_bottom_shorts_crowded_boundary():
    d = candle_series([100.0] * 120, 1440)
    assert run_coin(d, fut={"oi": None, "funding": -0.0001})["bottoms"]["shorts_crowded"]["on"]
    assert not run_coin(d, fut={"oi": None, "funding": -0.000099})["bottoms"]["shorts_crowded"]["on"]
    assert not run_coin(d, fut=None)["bottoms"]["shorts_crowded"]["on"]


def test_bottom_count_matches():
    s = run_coin(candle_series([100 * 0.99 ** i for i in range(120)], 1440), fut={"oi": None, "funding": -0.001})
    assert s["bottom_count"] == sum(1 for b in s["bottoms"].values() if b["on"]) and len(s["bottoms"]) == 6


def test_bottom_capitulation():
    def series(idx_from_end, vol_x, drop):
        closes = [100.0] * 120
        opens = [100.0] * 120
        vols = [1000.0] * 120
        i = 120 - idx_from_end
        opens[i], closes[i], vols[i] = 100.0, 100.0 * (1 - drop / 100), 1000.0 * vol_x
        if i + 1 < 120:
            opens[i + 1] = closes[i]
        return candle_series(closes, 1440, vols=vols, opens=opens)
    assert run_coin(series(2, 2.5, 5.0))["bottoms"]["capitulation"]["on"]      # yesterday, exactly at limits
    assert run_coin(series(4, 3.0, 6.0))["bottoms"]["capitulation"]["on"]      # 3 days ago
    assert not run_coin(series(5, 3.0, 6.0))["bottoms"]["capitulation"]["on"]  # 4 days ago: outside window
    assert not run_coin(series(1, 3.0, 6.0))["bottoms"]["capitulation"]["on"]  # today's unfinished candle
    assert not run_coin(series(2, 2.4, 6.0))["bottoms"]["capitulation"]["on"]  # volume too small
    assert not run_coin(series(2, 3.0, 4.9))["bottoms"]["capitulation"]["on"]  # drop too small


def test_ladder_prices_and_hits():
    s = run_coin(candle_series(flat_then([89.99]), 1440), anchor=100.0)
    prices = [r["price"] for r in s["ladder"]["rungs"]]
    for got, want in zip(prices, [90.0, 80.0, 70.0, 60.0]):
        close(got, want, 1e-9)
    assert [r["hit"] for r in s["ladder"]["rungs"]] == [True, False, False, False]
    s = run_coin(candle_series(flat_then([90.01]), 1440), anchor=100.0)
    assert not any(r["hit"] for r in s["ladder"]["rungs"])


def test_ladder_memory_and_reset():
    first = run_coin(candle_series(flat_then([95.0]), 1440), anchor=None)
    assert first["ladder"]["anchor"] == 95.0
    # a later run: a daily low of 76 after the anchor was set -> rungs 1 and 2 reached (85.5, 76.0)
    prev = dict(first, ladder=dict(first["ladder"], anchor_ts=NOW - 3 * DAY))
    lows = [99.0] * 119 + [76.0]
    s = run_coin(candle_series(flat_then([90.0]), 1440, lows=lows), prev=prev)
    assert s["ladder"]["anchor"] == 95.0 and s["ladder"]["low_since"] == 76.0
    assert [r["hit"] for r in s["ladder"]["rungs"]] == [True, True, False, False]
    # lows from before the anchor was set never count
    prev2 = dict(first, ladder=dict(first["ladder"], anchor_ts=NOW + DAY))
    s = run_coin(candle_series(flat_then([94.0]), 1440, lows=lows), prev=prev2)
    assert s["ladder"]["low_since"] == 94.0
    # changing the anchor in coins.json resets the memory
    s = run_coin(candle_series(flat_then([94.0]), 1440, lows=lows), prev=prev, anchor=200.0)
    assert s["ladder"]["anchor"] == 200.0 and s["ladder"]["low_since"] == 94.0
    assert [r["hit"] for r in s["ladder"]["rungs"]] == [True, True, True, True]    # 180/160/140/120 all above 94


def test_trailing_stop_and_drawdown():
    highs = [100.0] * 120
    highs[-30] = 200.0   # inside 90 days
    highs[-100] = 400.0  # outside 90 days, inside the 2-year window
    s = run_coin(candle_series([100.0] * 120, 1440, highs=highs))
    close(s["high_90d"], 200.0, 1e-9)
    close(s["trailing_stop"], 150.0, 1e-9)
    close(s["drawdown_pct"], -75.0, 1e-9)


def test_alerts_fire_once():
    s = {"symbol": "TST", "price": 100.0, "auto_flag_count": 3, "bottom_count": 3, "trailing_stop": 120.0,
         "bottoms": {"freefall": {"on": True}, "oversold": {"on": True}, "capitulation": {"on": True}},
         "flags": {"vertical": {"on": True}, "stretched": {"on": True}, "rsi": {"on": True}},
         "ladder": {"rungs": [{"n": 1, "price": 90, "drop": 10, "share": 25, "hit": True},
                              {"n": 2, "price": 80, "drop": 20, "share": 25, "hit": False}]},
         "alerted": {}}
    msgs = watch.build_alerts(s, first_run=False)
    assert len(msgs) == 3, msgs     # tier, rung 1, bottoms
    assert watch.build_alerts(s, first_run=False) == []     # same state -> silence
    s["auto_flag_count"] = 4
    assert len(watch.build_alerts(s, first_run=False)) == 1  # tier 1 -> 2
    s["auto_flag_count"] = 5
    assert watch.build_alerts(s, first_run=False) == []      # still tier 2
    s["bottom_count"] = 2
    assert watch.build_alerts(s, first_run=False) == []      # bottoms fell: no alert
    s["bottom_count"] = 3
    assert len(watch.build_alerts(s, first_run=False)) == 1  # back into the buy zone: alert again
    s2 = dict(s, alerted={})
    assert watch.build_alerts(s2, first_run=True) == []       # first run: no spam


def test_validate_rejects_bad_data():
    good = [{"t": NOW - (40 - i) * DAY, "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 1} for i in range(40)]
    watch.validate(good, "X", 1440)
    for bad in (good[:5], list(reversed(good)), good[:-1] + [dict(good[-1], l=12)],
                good[:-1] + [dict(good[-1], h=9)], [dict(c, t=c["t"] - 10 * DAY) for c in good],
                good[:-1] + [dict(good[-1], l=0)]):
        raises(lambda: watch.validate(bad, "X", 1440))


# =================================================================== news
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>
<item><title>Solana &amp; XRP &lt;b&gt;soar&lt;/b&gt;</title><link>https://a.com/1</link>
<pubDate>Fri, 20 Nov 2026 22:51:14 +0000</pubDate><description><![CDATA[<p>Big <b>day</b></p>]]></description></item>
<item><title>No date here</title><link>https://a.com/2</link></item>
<item><title>Bad link</title><link>javascript:alert(1)</link><pubDate>Fri, 20 Nov 2026 10:00:00 GMT</pubDate></item>
</channel></rss>"""
ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>x</title>
<entry><title>Ether ETF approved</title><link rel="alternate" href="https://b.com/e1"/><updated>2026-11-20T12:00:00Z</updated><summary>s</summary></entry>
</feed>"""
SITEMAP = b"""<?xml version="1.0"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9" xmlns:news="http://www.google.com/schemas/sitemap-news/0.9">
<url><loc>https://solana.com/news/x</loc><news:news><news:publication_date>2026-11-16T12:00:00.000Z</news:publication_date><news:title>Solana 1.0 launches</news:title></news:news></url>
</urlset>"""


def test_parse_feeds():
    r = news.parse_feed(RSS)
    assert [i["title"] for i in r] == ["Solana & XRP soar", "Bad link"]
    assert r[0]["ts"] == 1795215074 and r[0]["summary"] == "Big day" and r[1]["link"] == ""
    a = news.parse_feed(ATOM)
    assert a[0]["link"] == "https://b.com/e1" and a[0]["ts"] == 1795176000
    m = news.parse_feed(SITEMAP)
    assert m[0]["title"] == "Solana 1.0 launches" and m[0]["ts"] == 1794830400


def test_keyword_matching():
    sol = news.coin_keywords({"symbol": "SOL", "keywords": ["Solana", "SOL"]})
    hit = lambda t: any(rx.search(t) for rx in sol)  # noqa: E731
    for t in ("Solana rallies", "solana news", "$SOL jumps", "SOL/USD breaks out", "Why SOL's run matters"):
        assert hit(t), t
    for t in ("Sol Price, the musician", "solo trader wins", "SOLUSD", "console log", "Solaris"):
        assert not hit(t), t
    eth = news.coin_keywords({"symbol": "ETH", "keywords": ["Ethereum", "Ether", "ETH"]})
    h2 = lambda t: any(rx.search(t) for rx in eth)  # noqa: E731
    assert h2("Ether slides") and h2("ETHEREUM upgrade") and not h2("Ethena launches") and not h2("method")


def test_tags():
    assert news.tag_headline("Quant soars after bank partnership") == ["catalyst", "hype"]
    assert news.tag_headline("XRP price prediction: could hit $5?") == ["hype"]
    assert news.tag_headline("Exchange hacked, withdrawals paused") == ["risk"]
    assert news.tag_headline("Fed holds rates as inflation cools") == ["macro"]
    assert news.tag_headline("He was fed up with listings") == ["catalyst"]   # lower-case 'fed' is not the Fed
    assert news.tag_headline("Weekly market wrap") == []


COINS = [{"symbol": "SOL", "keywords": ["Solana", "SOL"]}, {"symbol": "XRP", "keywords": ["XRP"]},
         {"symbol": "ETH", "keywords": ["Ethereum", "ETH"]}]


def item(title, ts, link=None):
    return {"title": title, "link": link or "https://x.com/" + title.replace(" ", "-"), "ts": ts, "summary": ""}


def test_news_state_counts_dedupe_and_attribution():
    now = 1795800000
    src, off = {"name": "A"}, {"name": "SolOfficial", "coin": "SOL"}
    batch = [item("Solana and XRP surge", now - 3600), item("Fed meets", now - 7200),
             item("Random story", now - 10), item("Old SOL story", now - 8 * DAY),
             item("Future SOL story", now + 5000)]
    st = news.update_state(None, [(src, batch), (off, [item("Release notes", now - 60)])], COINS, now)
    today = news.day_of(now)
    assert st["daily"]["SOL"].get(today) == 3, st["daily"]       # surge + future(clamped) + official
    assert st["daily"]["XRP"].get(today) == 1 and st["daily"]["ETH"] == {}
    titles = {i["title"]: i for i in st["items"]}
    assert "Fed meets" in titles and titles["Fed meets"]["coins"] == []     # macro kept, not counted
    assert "Random story" not in titles and "Old SOL story" not in titles
    assert titles["Release notes"]["tags"][0] == "official" and titles["Release notes"]["coins"] == ["SOL"]
    assert titles["Future SOL story"]["ts"] == now
    st2 = news.update_state(json.loads(json.dumps(st)), [(src, batch)], COINS, now + 900)
    assert st2["daily"]["SOL"].get(today) == 3                   # same items again: no double count


def test_news_prune():
    now = 1795800000
    st = {"items": [{"id": "a", "title": "t", "link": "", "ts": now - 8 * DAY, "source": "A", "coins": ["SOL"], "tags": []}],
          "seen": {"old": news.day_shift(news.day_of(now), -22), "new": news.day_of(now)},
          "daily": {"SOL": {news.day_shift(news.day_of(now), -36): 5, news.day_shift(news.day_of(now), -10): 2}},
          "since": {"SOL": "2026-01-01"}}
    st = news.update_state(st, [], COINS, now)
    assert st["items"] == [] and set(st["seen"]) == {"new"}
    assert list(st["daily"]["SOL"].values()) == [2]


def make_state(now, since_days, base_counts, last3):
    today = news.day_of(now)
    daily = {}
    for k, n in base_counts.items():
        daily[news.day_shift(today, -k)] = n
    for k, n in enumerate(last3):
        daily[news.day_shift(today, -k)] = n
    return {"items": [], "seen": {}, "daily": {"SOL": daily}, "since": {"SOL": news.day_shift(today, -since_days)}}


def test_hype_metrics_math():
    now = 1795800000
    # tracked 20 days -> baseline days = today-20 .. today-3 = 18 days, 18 mentions total -> 1.0/day
    st = make_state(now, 20, {k: 1 for k in range(3, 21)}, [3, 3, 3])
    m = news.coin_metrics(st, "SOL", now, TH, None)
    assert m["baseline_days"] == 18 and m["baseline_per_day"] == 1.0 and m["mentions_3d"] == 9
    assert m["spike_ratio"] == 3.0 and m["spike"]                    # (9/3)/1.0 = 3x, >= 5 mentions
    st = make_state(now, 20, {k: 1 for k in range(3, 21)}, [3, 3, 2])
    assert not news.coin_metrics(st, "SOL", now, TH, None)["spike"]  # 2.67x
    # quiet coin: zero baseline uses a floor of 1 mention per 3 days
    st = make_state(now, 20, {}, [2, 2, 1])
    m = news.coin_metrics(st, "SOL", now, TH, None)
    assert m["spike_ratio"] == 5.0 and m["spike"]                    # (5/3)/(1/3)
    st = make_state(now, 20, {}, [2, 1, 1])
    assert not news.coin_metrics(st, "SOL", now, TH, None)["spike"]  # only 4 mentions (< 5 minimum)
    # baseline window never looks back more than 30 days
    st = make_state(now, 90, {40: 99}, [0, 0, 0])
    m = news.coin_metrics(st, "SOL", now, TH, None)
    assert m["baseline_days"] == 28 and m["baseline_per_day"] == 0.0
    # cold start (< 7 baseline days): needs 8+ mentions in 3 days
    st = make_state(now, 5, {}, [3, 3, 2])
    m = news.coin_metrics(st, "SOL", now, TH, None)
    assert m["baseline_days"] == 3 and m["spike_ratio"] is None and m["spike"]
    st = make_state(now, 5, {}, [3, 3, 1])
    assert not news.coin_metrics(st, "SOL", now, TH, None)["spike"]
    # 14-day bar data ends today, oldest first
    assert m["daily14"][-1][0] == news.day_of(now) and len(m["daily14"]) == 14


def test_news_flags_boundaries():
    m = {"spike": True, "catalysts_7d": 1, "hype_3d": 0, "trending_rank": None, "mentions_3d": 9,
         "spike_ratio": 3.0, "baseline_days": 10}
    f = lambda **k: news.news_flags(dict(m, **{x: v for x, v in k.items() if x in m}),  # noqa: E731
                                    k.get("ch7", 30), TH, k.get("ok", True), k.get("tok", True))
    assert f(ch7=25)["news"]["on"] and not f(ch7=24.99)["news"]["on"]
    assert not f(catalysts_7d=0)["news"]["on"] and not f(spike=False)["news"]["on"]
    assert not f(ok=False)["news"]["on"]
    assert not f(ch7=None)["news"]["on"]
    assert f(ch7=15, trending_rank=4)["fomo"]["on"] and not f(ch7=14.99, trending_rank=4)["fomo"]["on"]
    assert f(ch7=15, hype_3d=3)["fomo"]["on"] and not f(ch7=15, hype_3d=2)["fomo"]["on"]
    assert not f(ch7=15, hype_3d=5, ok=False)["fomo"]["on"]           # hype count needs working feeds
    assert f(ch7=15, trending_rank=1, ok=False)["fomo"]["on"]         # trending still works alone
    assert "unavailable" in f(tok=False)["fomo"]["detail"]


# =================================================================== versions
def test_versions_match():
    root = os.path.join(HERE, "..")
    v = open(os.path.join(root, "VERSION")).read().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), v
    app = open(os.path.join(root, "site", "index.html")).read()
    assert f'const APP_VERSION = "{v}";' in app, "site/index.html and VERSION disagree"
    assert watch.VERSION == v
    assert f"## {v}" in open(os.path.join(root, "CHANGELOG.md")).read(), "CHANGELOG has no entry for this version"


# =================================================================== end to end
def test_end_to_end_offline_runs():
    root = os.path.join(HERE, "..")
    cfg = json.load(open(os.path.join(root, "coins.json")))
    if not any(c["symbol"] == "QNT" for c in cfg["coins"]):   # the offline feed has a QNT pump in it
        cfg["coins"].append({"symbol": "QNT", "name": "Quant", "kraken": "QNTUSD", "futures": "",
                             "coingecko": "quant-network", "keywords": ["Quant", "QNT"], "anchor": None})
    with tempfile.TemporaryDirectory() as td:
        cpath = os.path.join(td, "c.json")
        json.dump(cfg, open(cpath, "w"))
        env = dict(os.environ, DEMO_OFFLINE="1")
        env.pop("TELEGRAM_BOT_TOKEN", None)
        outs = []
        for i in range(2):
            out = os.path.join(td, f"r{i}", "signals.json")
            args = [sys.executable, os.path.join(root, "watcher", "watch.py"), "--config", cpath, "--out", out,
                    "--prev", outs[-1] if outs else os.path.join(td, "none.json")]
            subprocess.run(args, check=True, env=env, capture_output=True)
            outs.append(out)
        a, b = (json.load(open(p)) for p in outs)
        for doc in (a, b):
            assert doc["version"] == watch.VERSION
            assert len(doc["coins"]) == len(cfg["coins"])
            for c in doc["coins"]:
                assert c["auto_flag_count"] == sum(1 for f in c["flags"].values() if f["on"])
                assert set(c["flags"]) == {"vertical", "stretched", "rsi", "leverage", "news", "fomo"}
                assert c["bottom_count"] == sum(1 for x in c["bottoms"].values() if x["on"])
                assert set(c["bottoms"]) == {"freefall", "stretched_down", "oversold", "shorts_crowded", "capitulation", "near_200w"}
                for r, cfg_r in zip(c["ladder"]["rungs"], cfg["ladder"]):
                    close(r["price"], c["ladder"]["anchor"] * (1 - cfg_r["drop"] / 100), 1e-9)
                close(c["trailing_stop"], c["high_90d"] * 0.75, 1e-9)
                assert os.path.exists(os.path.join(os.path.dirname(outs[0]), "candles", c["symbol"] + ".json"))
        assert a["news"]["daily"] == b["news"]["daily"], "second run double-counted headlines"
        q = next(c for c in b["coins"] if c["symbol"] == "QNT")
        assert q["flags"]["fomo"]["on"] and q["news"]["trending_rank"] == 1


if __name__ == "__main__":
    fails = 0
    tests = [(n, f) for n, f in list(globals().items()) if n.startswith("test_")]
    for name, fn in tests:
        try:
            fn()
            print("ok  ", name)
        except Exception as e:  # noqa: BLE001
            fails += 1
            print("FAIL", name, type(e).__name__, e)
    print(f"{len(tests) - fails}/{len(tests)} passed")
    sys.exit(1 if fails else 0)
