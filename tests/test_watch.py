"""Crypto Coach maths checks. Runs before every watcher run; if anything fails,
nothing is published. Plain Python, no installs:  python3 tests/test_watch.py

Three layers:
  1. reference values   - known published answers (RSI) and hand-worked examples
  2. cross-checks       - a second, independent implementation compared on random data
  3. scenarios          - every flag, signal, ladder and alert tested at its exact boundary
"""
import json
from datetime import datetime
import math
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
sys.path.insert(0, os.path.join(HERE, "..", "backtest"))
import accumulate as acc  # noqa: E402
import history  # noqa: E402
import stress  # noqa: E402
import myplan  # noqa: E402

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


def test_trailing_stop_and_drawdown():
    highs = [100.0] * 120
    highs[-30] = 200.0   # inside 90 days
    highs[-100] = 400.0  # outside 90 days, inside the 2-year window
    s = run_coin(candle_series([100.0] * 120, 1440, highs=highs))
    close(s["high_90d"], 200.0, 1e-9)
    close(s["trailing_stop"], 150.0, 1e-9)
    close(s["drawdown_pct"], -75.0, 1e-9)


def test_alerts_fire_once():
    s = {"symbol": "TST", "price": 100.0, "auto_flag_count": 3, "bottom_count": 3,
         "flags": {"vertical": {"on": True}, "stretched": {"on": True}, "rsi": {"on": True}},
         "bottoms": {"freefall": {"on": True}, "oversold": {"on": True}, "capitulation": {"on": True}},
         "new_events": [{"kind": "take_profit", "price": 120.0, "pct": 20, "share": 50}],
         "alerted": {}}
    msgs = watch.build_alerts(s, first_run=False)
    assert [p for _, p in msgs] == [3, 3], msgs                 # v5: heads-ups only, no old JUMP order alerts
    assert "overheated" in msgs[0][0] and "washed out" in msgs[1][0]
    assert watch.build_alerts(s, first_run=False) == []          # same state -> silence
    s["auto_flag_count"] = 4
    assert len(watch.build_alerts(s, first_run=False)) == 1      # heads-up level 1 -> 2
    s["bottom_count"] = 2
    assert watch.build_alerts(s, first_run=False) == []
    s["bottom_count"] = 3
    assert len(watch.build_alerts(s, first_run=False)) == 1
    assert watch.build_alerts(dict(s, alerted={}), first_run=True) == []   # first run: no spam


def test_daily_summary_once_a_day_at_the_set_hour():
    from zoneinfo import ZoneInfo
    plan = {"days": [1, 15], "summary_hour": 18, "timezone": "America/Vancouver"}
    tz = ZoneInfo("America/Vancouver")
    at = lambda y, m, d, h: datetime(y, m, d, h, 5, tzinfo=tz).timestamp()
    coins = [{"symbol": "SOL", "price": 133.0, "change_24h": -4.7, "ma200": 150.0, "ma200_prev": 160.0,
              "auto_flag_count": 0, "bottom_count": 1}]
    msg, dg = watch.daily_summary(coins, plan, {}, [], now=at(2026, 11, 22, 17))
    assert msg is None and dg == {}                               # before 6 pm: nothing
    msg, dg = watch.daily_summary(coins, plan, {}, [], now=at(2026, 11, 22, 18))
    assert msg[1] == 3 and "Nothing to do" in msg[0] and "🐻 bear" in msg[0] and dg == {"last_date": "2026-11-22"}
    msg2, _ = watch.daily_summary(coins, plan, dg, [], now=at(2026, 11, 22, 21))
    assert msg2 is None                                           # once a day
    msg, dg = watch.daily_summary(coins, plan, dg, [("🟡 HEADS UP · SOL looks washed out", 3)], now=at(2026, 12, 1, 18))
    assert msg[1] == 4 and "BUY DAY: SOL" in msg[0] and "HEADS UP" in msg[0]
    assert watch.trend_word({"price": 200, "ma200": 150, "ma200_prev": 140}) == "🐂 bull"
    assert watch.trend_word({"price": 140, "ma200": 150, "ma200_prev": 140}) == "↔ sideways"


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


# =================================================================== v4 orders engine
sys.path.insert(0, os.path.join(HERE, "..", "watcher"))
import engine  # noqa: E402
R = engine.DEFAULT_RULES


def test_engine_starting_orders():
    st = engine.new_state(100.0, 100.0, R)
    o = engine.open_orders(st, R)
    assert [(x["kind"], round(x["price"], 6)) for x in o] == [
        ("take_profit", 120.0), ("take_profit", 140.0),
        ("dip_buy", 90.0), ("dip_buy", 75.0), ("dip_buy", 60.0), ("dip_buy", 45.0)]


def test_engine_take_profit_then_buy_backs_then_new_cycle():
    st = engine.new_state(100.0, 100.0, R)
    ev = engine.step(st, 119.99, 100.0, 1, R)
    assert ev == []                                                   # just under +20%: nothing
    ev = engine.step(st, 121.0, 110.0, 2, R)
    assert [(e["kind"], e["price"]) for e in ev] == [("take_profit", 120.0)]
    bb = [round(x["price"], 6) for x in engine.open_orders(st, R) if x["kind"] == "buy_back"]
    assert bb == [96.0, 72.0]                                         # 20% and 40% below the $120 sale
    ev = engine.step(st, 141.0, 95.0, 3, R)                           # buy-back fills; the old +40% is gone
    assert [(e["kind"], round(e["price"], 6)) for e in ev] == [("buy_back", 96.0)]
    # the buy-back started a new cycle measured from $96: take-profits now 115.20 and 134.40,
    # placed just now, so even a high of 141 in the same period doesn't fill them
    assert st["cycle"] == 2 and abs(st["ref"] - 96.0) < 1e-9
    ev = engine.step(st, 141.0, 120.0, 4, R)
    assert [(e["kind"], round(e["price"], 6)) for e in ev] == [("take_profit", 115.2), ("take_profit", 134.4)]


def test_engine_new_orders_wait_a_step():
    st = engine.new_state(100.0, 100.0, R)
    ev = engine.step(st, 125.0, 90.0, 1, R)        # TP fills and dip rung 1 fills; buy-backs (96, 72) just created
    assert sorted(e["kind"] for e in ev) == ["dip_buy", "take_profit"]
    assert not any(e["kind"] == "buy_back" for e in ev)              # 90 < 96, but the order didn't exist yet
    ev = engine.step(st, 100.0, 95.0, 2, R)
    assert [(e["kind"], e["price"]) for e in ev] == [("buy_back", 96.0)]


def test_engine_reference_is_average_buy_back_price():
    st = engine.new_state(100.0, 100.0, R)
    engine.step(st, 120.0, 110.0, 1, R)            # sell at 120 -> buy-backs 96 / 72
    engine.step(st, 100.0, 70.0, 2, R)             # both fill
    # equal-dollar buys at 96 and 72 -> average cost 2/(1/96 + 1/72) = 82.2857...
    assert abs(st["ref"] - 2 / (1 / 96 + 1 / 72)) < 1e-9
    assert not st["buyback_sets"]
    tps = [round(x["price"], 6) for x in engine.open_orders(st, R) if x["kind"] == "take_profit"]
    assert tps == [round(st["ref"] * 1.2, 6), round(st["ref"] * 1.4, 6)]


def test_engine_moved_take_profits_wait_a_step():
    # Found in the real SOL backtest (10 Oct 2025): a 2nd buy-back lowered the average, and the
    # re-priced take-profit "filled" on the same candle. It can't: that order didn't exist yet.
    st = engine.new_state(100.0, 100.0, R)
    engine.step(st, 120.0, 110.0, 1, R)            # sell at 120 -> buy-backs 96 / 72
    engine.step(st, 100.0, 95.0, 2, R)             # buy-back 96 fills -> new cycle, base 96, TPs 115.2 / 134.4
    ev = engine.step(st, 115.5, 70.0, 3, R)        # 72 fills -> base 82.29, TP1 now 98.74; high 115.5 is above both
    assert "take_profit" not in [e["kind"] for e in ev] and "buy_back" in [e["kind"] for e in ev], ev
    ev = engine.step(st, 99.0, 90.0, 4, R)
    assert [e["kind"] for e in ev] == ["take_profit"] and abs(ev[0]["price"] - st["ref"] * 1.2) < 1e-9


def test_engine_dip_ladder_independent_of_reference():
    st = engine.new_state(100.0, 100.0, R)
    ev = engine.step(st, 100.0, 59.0, 1, R)
    assert [e["rung"] for e in ev] == [1, 2, 3] and st["ref"] == 100.0   # dip buys don't move take-profits
    ev = engine.step(st, 100.0, 59.0, 2, R)
    assert ev == []                                                       # each rung fires once


def test_engine_config_changes_restart_parts():
    st = engine.new_state(100.0, 100.0, R)
    engine.step(st, 125.0, 88.0, 1, R)
    engine.sync_config(st, 100.0, 100.0, R)        # unchanged -> nothing resets
    assert st["tp_done"][0] and st["dip_done"][0]
    engine.sync_config(st, 80.0, 50.0, R)          # you edited cost and anchor
    assert st["ref"] == 80.0 and st["tp_done"] == [False, False]
    assert st["anchor"] == 50.0 and st["dip_done"] == [False] * 4


def test_engine_in_watcher_persists_and_never_repeats():
    d = candle_series(flat_then([100.0]), 1440)
    s1 = run_coin(d)
    assert s1["new_events"] == [] and s1["engine"]["ref"] == 100.0          # first run: orders only
    prev = json.loads(json.dumps(s1))
    prev["engine"]["last_ts"] = NOW - 3 * 3600
    h1 = candle_series([100.0] * 39 + [100.0], 60, highs=[100.0] * 39 + [121.0])
    orig = (watch.candles, watch.futures_info)
    series = {1440: d, 10080: candle_series([100.0] * 60, 10080), 240: candle_series([100.0] * 40, 240), 60: h1}
    watch.candles = lambda pair, interval: series[interval]
    watch.futures_info = lambda sym: None
    try:
        coin = {"symbol": "TST", "kraken": "X", "futures": "", "anchor": None, "cost": None}
        s2 = watch.score_coin(coin, TH, LADDER, prev, R)
        assert [e["kind"] for e in s2["new_events"]] == ["take_profit"]
        s3 = watch.score_coin(coin, TH, LADDER, json.loads(json.dumps(s2)), R)
        assert s3["new_events"] == []                                       # same high again: no repeat
    finally:
        watch.candles, watch.futures_info = orig


def test_notify_without_channels_is_safe():
    import notify
    for k in ("NTFY_TOPIC", "TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID", "CALLMEBOT_PHONE", "CALLMEBOT_APIKEY"):
        os.environ.pop(k, None)
    assert notify.channels() == []
    notify.send_all([("test", 3)])                                          # prints, never raises


# =================================================================== backtest engine
sys.path.insert(0, os.path.join(HERE, "..", "backtest"))
import backtest as BT  # noqa: E402


def bt_series(closes, lows=None):
    t0 = 1_700_000_000 - 1_700_000_000 % DAY
    return [{"t": t0 + i * DAY, "o": c, "h": c, "l": (lows[i] if lows else c), "c": c, "v": 1.0} for i, c in enumerate(closes)]


def test_backtest_ladder_fills_at_rung_prices():
    closes = [100.0] * 10 + [100 - 0.5 * k for k in range(1, 101)]          # slides from 100 to 50
    d = bt_series(closes)
    r = BT.simulate(d, 9, len(d) - 1, lambda i: ([], []), LADDER, TH, buy_early=False)
    buys = [e for e in r["events"] if e[3] > 0]
    assert [e[2] for e in buys] == [90.0, 80.0, 70.0, 60.0], buys            # anchor 100, rungs -10..-40%
    assert all(abs(e[3] - 125.0) < 1e-9 for e in buys)                      # 25% of $500 each
    want = 1000 / 100 + sum(125 * (1 - BT.FEE) / p for p in (90, 80, 70, 60))
    close(r["coins"], want, 1e-5)
    close(r["value"], want * 50.0 + r["cash"], 0.02)
    assert r["cash"] == 0.0


def test_backtest_buy_early_and_half_rungs():
    closes = [100.0] * 10 + [95.0] * 5
    d = bt_series(closes)
    sig = lambda i: ([], ["a", "b", "c"] if i == 12 else [])                 # noqa: E731  3 bottom signals on one day
    r = BT.simulate(d, 9, len(d) - 1, sig, LADDER, TH)
    assert [(e[1][:9], e[2], e[3]) for e in r["events"]] == [("buy early", 95.0, 125.0)]
    r = BT.simulate(d, 9, len(d) - 1, sig, LADDER, TH, boost_fraction=0.5)
    assert r["events"][0][3] == 62.5                                        # half a rung
    d2 = bt_series(closes + [85.0])                                         # then the price reaches rung 1 (90)
    r = BT.simulate(d2, 9, len(d2) - 1, sig, LADDER, TH, boost_fraction=0.5)
    assert [(e[2], e[3]) for e in r["events"]] == [(95.0, 62.5), (90.0, 62.5)]   # the other half fills at the rung


def test_backtest_trim_and_trailing_stop():
    closes = [100.0] * 10 + [200.0, 200.0, 100.0]
    d = bt_series(closes)
    sig = lambda i: (["vertical", "stretched", "rsi"] if i == 10 else [], [])  # noqa: E731
    r = BT.simulate(d, 9, len(d) - 1, sig, LADDER, TH)
    sells = [e for e in r["events"] if e[3] < 0]
    assert len(sells) == 2 and r["trims"] == 1
    close(-sells[0][4], 1.5, 1e-9)                        # 25% of the 6-coin trade bucket (40% core)
    assert sells[1][1] == "trailing stop" and sells[1][2] == 100.0   # 25%+ below the 200 peak
    close(r["coins"], 4.0, 1e-9)                          # core untouched, trade bucket emptied


def test_backtest_reanchor_after_trim():
    # trim at 200 on day 10, then the price falls to 150: the new ladder (from 200) buys at 180 and 160
    closes = [100.0] * 10 + [200.0, 175.0, 155.0]
    d = bt_series(closes)
    sig = lambda i: (["vertical", "stretched", "rsi"] if i == 10 else [], [])  # noqa: E731
    r = BT.simulate(d, 9, len(d) - 1, sig, LADDER, TH, buy_early=False, reanchor_on_trim=True)
    ev = r["events"]
    assert ev[0][1].startswith("trim") and ev[0][2] == 200.0
    cash_after_trim = 500 + (-ev[0][3])
    buys = [(e[2], e[3]) for e in ev[1:] if e[3] > 0]
    assert [b[0] for b in buys] == [180.0, 160.0], ev
    for _, usd in buys:
        close(usd, cash_after_trim * 0.25, 0.011)          # each rung = 25% of the cash at the trim
    plain = BT.simulate(d, 9, len(d) - 1, sig, LADDER, TH, buy_early=False)
    assert not [e for e in plain["events"] if e[3] > 0]    # without moving, the ladder (from 100) never fills


def test_backtest_hybrid_never_overspends():
    closes = [100.0] * 10 + [100 - 0.1 * k for k in range(1, 560)]
    d = bt_series(closes)
    deep = [{"drop": 10, "share": 25}, {"drop": 25, "share": 25}, {"drop": 40, "share": 25}, {"drop": 55, "share": 25}]
    r = BT.simulate(d, 9, len(d) - 1, lambda i: ([], []), deep, TH, buy_early=False, reanchor_on_trim=True, dca_share=0.5)
    sched = [e for e in r["events"] if e[1] == "scheduled monthly buy"]
    rungs = [e for e in r["events"] if e[1].startswith("ladder")]
    assert len(sched) == round((len(d) - 10) / 30.4375)
    close(sum(e[3] for e in sched), 250.0, 0.05)                # half the reserve on the schedule
    assert [e[2] for e in rungs] == [90.0, 75.0, 60.0, 45.0]    # deep ladder, price ends at 44.1
    assert sum(e[3] for e in r["events"]) <= 500.0 + 0.1 and r["cash"] >= -1e-9   # (+ cent rounding in the log)
    close(r["coin_equiv"], r["coins"] + r["cash"] / d[-1]["c"], 1e-6)


def test_backtest_take_profit_method():
    # 10 coins at $100 (4 core, 6 trade). TPs at +10% / +20% of avg cost, 50% each; buy-backs 10% / 20% below the sale.
    closes = [100.0] * 10 + [105.0, 115.0, 125.0, 108.0, 96.0]
    highs = [100.0] * 10 + [105.0, 111.0, 121.0, 125.0, 108.0]
    t0 = 1_700_000_000 - 1_700_000_000 % DAY
    d = [{"t": t0 + i * DAY, "o": c, "h": h, "l": min(c, closes[i - 1] if i else c), "c": c, "v": 1.0}
         for i, (c, h) in enumerate(zip(closes, highs))]
    r = BT.simulate_tp(d, 9, len(d) - 1, lambda i: ([], []), TH, [10, 20], 0.5, BT.half2(10), LADDER)
    ev = r["events"]
    assert [(e[1], e[2]) for e in ev[:2]] == [("take-profit +10% hit", 110.0), ("take-profit +20% hit", 120.0)], ev
    close(-ev[0][4], 3.0, 1e-9) and close(-ev[1][4], 3.0, 1e-9)        # 3 + 3 of the 6 trade coins
    close(-ev[0][3], 3 * 110 * (1 - BT.FEE), 1e-2)
    buys = [e for e in ev if e[3] > 0]
    assert [e[2] for e in buys] == [108.0, 96.0], ev                     # 10% and 20% below the $120 sale
    cash_at_sale = 500 + 3 * 110 * (1 - BT.FEE) + 3 * 120 * (1 - BT.FEE)
    close(buys[0][3], cash_at_sale * 0.5, 0.02)
    assert r["cash"] >= -1e-9 and r["trims"] == 2


def test_backtest_v4_amounts():
    # $1,000 at $100 = 10 coins (4 core, 6 trade), $500 dip reserve. Up to 121 (TP +20% at 120),
    # down to 95 (buy-back at 96), back up to 100.
    closes = [100.0] * 10 + [121.0, 95.0, 100.0]
    t0 = 1_700_000_000 - 1_700_000_000 % DAY
    d = [{"t": t0 + i * DAY, "o": c, "h": c, "l": c, "c": c, "v": 1.0} for i, c in enumerate(closes)]
    r = BT.simulate_v4(d, 9, len(d) - 1)
    ev = r["events"]
    assert ev[0][1] == "JUMP OUT: take-profit +20%" and ev[0][2] == 120.0
    close(-ev[0][4], 3.0, 1e-9)                                  # 50% of the 6 trade coins
    got = 3 * 120 * (1 - BT.FEE)
    close(-ev[0][3], got, 0.01)
    assert ev[1][1] == "JUMP IN: buy-back" and ev[1][2] == 96.0
    close(ev[1][3], got * 0.5, 0.01)                              # half the sale's money
    close(r["coins"], 4 + 3 + got * 0.5 * (1 - BT.FEE) / 96.0, 1e-6)
    close(r["cash"], 500 + got * 0.5, 0.01)


def test_backtest_benchmarks():
    d = bt_series([100.0] * 10 + [50.0] * 400)
    lump = BT.simple(d, 9, len(d) - 1, "lump")
    close(lump["coins"], 10 + 500 * (1 - BT.FEE) / 100, 1e-9)
    dca = BT.simple(d, 9, len(d) - 1, "dca")
    assert len(dca["events"]) == round((len(d) - 10) / 30.4375) and abs(dca["cash"]) < 1e-6
    cash = BT.simple(d, 9, len(d) - 1, "cash")
    assert cash["coins"] == 10 and cash["value"] == 10 * 50 + 500


# =================================================================== versions
def test_versions_match():
    root = os.path.join(HERE, "..")
    v = open(os.path.join(root, "VERSION")).read().strip()
    assert re.fullmatch(r"\d+\.\d+\.\d+", v), v
    app = open(os.path.join(root, "site", "index.html")).read()
    assert f'const APP_VERSION = "{v}";' in app, "site/index.html and VERSION disagree"
    assert watch.VERSION == v
    assert f"## {v}" in open(os.path.join(root, "CHANGELOG.md")).read(), "CHANGELOG has no entry for this version"


# =================================================================== accumulation backtest (v5 models)
T0 = 1735689600  # 2025-01-01 00:00 UTC


def acc_days(rows):
    """rows of (o, h, l, c) -> daily candles from 1 Jan 2025."""
    return [{"t": T0 + k * 86400, "o": o, "h": h, "l": l, "c": c, "v": 1.0} for k, (o, h, l, c) in enumerate(rows)]


def no_signals(i):
    return ([], [])


def test_acc_dca_buys_each_month():
    d = acc_days([(10, 10, 10, 10)] * 90)           # Jan, Feb, Mar
    r = acc.run(d, 0, 89, no_signals, "dca")
    assert r["contributed"] == 300 and r["buys"] == 3 and r["sells"] == 0
    close(r["core"], 3 * 100 * (1 - acc.FEE) / 10, 1e-9)
    close(r["coin_equiv"], r["core"], 1e-9)


def test_acc_take_profit_buy_back_and_gain_to_core():
    b = acc.Book()
    b.cash = 1000.0
    b.cash_lots = [[0, 1000.0]]
    b.buy_in(1000, 100, T0, "test")
    q = 1000 * (1 - acc.FEE) / 100
    close(b.core, q * 0.4, 1e-12)
    close(b.trade, q * 0.6, 1e-12)
    b.reset_tp()
    base = 600 / (q * 0.6)
    close(b.tp["base"], base, 1e-9)
    # day 1: high hits +20%, low would hit the buy-back too - but the buy-back didn't exist yet
    b.fill(b.orders(), {"t": T0 + 86400, "h": base * 1.25, "l": 1.0})
    tp1 = base * 1.2
    sold = q * 0.6 * 0.5
    close(b.trade, q * 0.6 - sold, 1e-12)
    assert b.sells == 1 and b.buys == 1 and len(b.sales) == 1
    got = sold * tp1 * (1 - acc.FEE)
    lv = b.sales[0]["levels"]
    close(lv[0]["price"], tp1 * 0.8, 1e-9)
    close(lv[1]["price"], tp1 * 0.6, 1e-9)
    close(lv[0]["usd"], got / 2, 1e-9)
    core_before = b.core
    # day 2: both buy-backs fill; coins bought back beyond what was sold go to Core
    b.fill(b.orders(), {"t": T0 + 2 * 86400, "h": 1.0, "l": tp1 * 0.5})
    rebought = got / 2 * (1 - acc.FEE) / (tp1 * 0.8) + got / 2 * (1 - acc.FEE) / (tp1 * 0.6)
    assert rebought > sold
    close(b.core - core_before, rebought - sold, 1e-12)
    close(b.trade, q * 0.6 - sold + sold, 1e-12)      # trade bucket back to its size before the sale
    assert b.sales == [] and b.buys == 3


def test_acc_new_take_profit_waits_a_day():
    b = acc.Book()
    b.cash = 1000.0
    b.cash_lots = [[0, 1000.0]]
    b.buy_in(1000, 100, T0, "test")
    snap = b.orders()                                  # no take-profit yet: it is set at the close
    assert snap == []
    b.fill(snap, {"t": T0, "h": 1000.0, "l": 100.0})
    assert b.sells == 0


def test_acc_model_a_rung_and_money_conservation():
    rows = [(100, 100, 100, 100)] * 31 + [(100, 100, 89, 95)] + [(95, 96, 94, 95)] * 30
    d = acc_days(rows)
    r = acc.run(d, 30, len(d) - 1, no_signals, "A")
    first = r["events"][0]
    assert first[0] == "2025-02-01" and first[1].startswith("JUMP IN: dip rung 1")
    close(first[2], 90.0, 1e-9)                        # 10% below the 30-day high of 100
    close(first[3], 200 / 4, 1e-9)                      # Jan + Feb money waiting, a quarter of it


def test_acc_b_buys_next_open_after_turn():
    rows = [(100 - k, 100 - k, 99 - k, 100 - k) for k in range(40)]
    rows += [(61, 75, 61, 74), (77, 78, 76, 77)] + [(77, 78, 76, 77)] * 5
    d = acc_days(rows)
    r = acc.run(d, 20, len(d) - 1, lambda i: ([], ["a", "b", "c"]), "B_strict")
    first = r["events"][0]
    assert first[1] == "JUMP IN: washed out + turning up"
    assert first[0] == acc.day(d[41]["t"])            # signal at day 40's close -> next day's open
    close(first[2], 77.0, 1e-9)


def test_acc_money_waits_at_most_60_days():
    rows = [(100 + k * 0.1, 100 + k * 0.1, 100 + k * 0.1, 100 + k * 0.1) for k in range(120)]
    d = acc_days(rows)
    r = acc.run(d, 0, 119, no_signals, "A")
    first = r["events"][0]
    assert first[1].startswith("Waited 60 days") and first[0] == acc.day(d[60]["t"])
    close(first[3], 100.0, 1e-9)                       # only January's money was that old


def test_acc_lots_each_buy_has_its_own_take_profits():
    b = acc.LotBook()
    b.cash = 2000.0
    b.cash_lots = [[0, 2000.0]]
    b.buy_in(1000, 100, T0, "lot 1")
    b.buy_in(1000, 80, T0, "lot 2")
    b.end_day()
    tps = sorted(px for kind, ref, px in b.orders() if kind == "tp")
    c1 = 600 / (1000 * (1 - acc.FEE) / 100 * 0.6)       # lot cost per coin, fee included
    c2 = 600 / (1000 * (1 - acc.FEE) / 80 * 0.6)
    for got, want in zip(tps, sorted([c1 * 1.2, c1 * 1.4, c2 * 1.2, c2 * 1.4])):
        close(got, want, 1e-9)
    # price reaches only the cheap lot's +20%: only that lot sells, half of it
    q2 = 1000 * (1 - acc.FEE) / 80 * 0.6
    trade0 = b.trade
    b.fill(b.orders(), {"t": T0 + 86400, "h": c2 * 1.25, "l": c2 * 1.1})
    assert b.sells == 1
    close(trade0 - b.trade, q2 / 2, 1e-12)


def test_acc_minimum_order_size():
    b = acc.Book()
    b.cash = 4.0
    b.cash_lots = [[0, 4.0]]
    b.buy_in(4.0, 10, T0, "too small")
    assert b.buys == 0 and b.cash == 4.0                  # under $5 isn't bought
    lv = acc.Book.buyback_levels(100.0, 8.0)                # $4 halves would be too small -> one order with all $8
    assert len(lv) == 1 and lv[0]["usd"] == 8.0
    close(lv[0]["price"], 80.0, 1e-12)
    assert len(acc.Book.buyback_levels(100.0, 20.0)) == 2


def test_acc_money_and_coins_add_up():
    rng = random.Random(7)
    rows, px = [], 100.0
    for _ in range(500):
        o = px
        px *= math.exp(rng.gauss(0, 0.05))
        rows.append((o, max(o, px) * 1.02, min(o, px) * 0.98, px))
    d = acc_days(rows)
    for m in acc.MODELS:
        r = acc.run(d, 40, 499, lambda i: ([], ["x"] * (i % 4)), m)
        spent = sum(e[3] for e in r["events"])
        close(r["contributed"] - spent, r["cash"], 0.01 + 0.005 * len(r["events"]))  # every dollar spent or waiting (cent rounding per trade)
        coins = sum(e[4] for e in r["events"] if not e[1].startswith("Gain"))
        close(coins, r["coins"], 1e-6)                           # every coin came from a logged trade
        assert r["core"] >= -1e-12 and r["trade"] >= -1e-12 and r["cash"] >= -0.01


# =================================================================== stress test (v5 rules, long history)
def st_series(rows, t0=T0):
    return stress.Series([{"t": t0 + k * 86400, "o": o, "h": h, "l": l, "c": c, "v": 1.0} for k, (o, h, l, c) in enumerate(rows)])


def test_stress_rung_fills_at_gap_open_with_maker_fee():
    s = st_series([(100, 100, 100, 100)] * 36 + [(80, 81, 79, 80)] + [(80, 80, 80, 80)] * 5)
    sim = stress.Sim(s, 35, 41, stress.BASE, record=True)
    sim.run()
    e = sim.events[0]
    assert e[1] == "buy-in rung 1"
    close(e[2], 80.0, 1e-12)                       # the 90 limit filled at the lower open, not at 90
    close(e[3], 500 / 4, 1e-9)                     # a quarter of the $500 waiting
    close(e[4], 125 * (1 - stress.MAKER) / 80, 1e-12)


def test_stress_passed_rungs_market_one_per_week():
    rows = [(100, 100, 100, 100)] * 39 + [(50, 50, 50, 50)] * 20
    s = st_series(rows)
    sim = stress.Sim(s, 39, 58, stress.BASE, record=True)
    sim.run()
    buys = [e for e in sim.events if "already passed" in e[1]]
    assert [e[1][:13] for e in buys] == ["buy-in rung 1", "buy-in rung 2", "buy-in rung 3"]
    days = [datetime.strptime(e[0], "%Y-%m-%d") for e in buys]
    assert days[0] == datetime.strptime(stress.ymd(s.d[40]["t"]), "%Y-%m-%d")   # next open after arming
    assert all((b - a).days >= 7 for a, b in zip(days, days[1:]))
    for e in buys:
        close(e[3], 125.0, 1e-9)                    # 1/4, then 1/3 of the rest, then 1/2 of the rest
        close(e[4], 125 * (1 - stress.TAKER) / 50, 1e-12)


def test_stress_three_buyins_a_month_max():
    s = st_series([(100, 100, 100, 100)] * 150)
    sim = stress.Sim(s, 10, 149, stress.BASE, entry="random", p_random=1.0, seed=1)
    sim.run()
    assert max(sim.month_buyins.values()) == 3


def test_stress_lot_modes():
    s = st_series([(100, 100, 100, 100)] * 70)     # Jan 1 .. Mar 11
    for mode, want in (("buy", 3), ("month", 2), ("avg", 1)):
        sim = stress.Sim(s, 0, 69, dict(stress.BASE, lots=mode))
        sim.add_trade(2, 1.0, 100.0)               # January
        sim.add_trade(20, 1.0, 120.0)              # January
        sim.add_trade(40, 1.0, 90.0)               # February
        assert len(sim.lots) == want, (mode, len(sim.lots))
    sim = stress.Sim(s, 0, 69, dict(stress.BASE, lots="month"))
    sim.add_trade(2, 1.0, 100.0)
    sim.add_trade(20, 1.0, 120.0)
    close(sim.lots[0]["cost"] / sim.lots[0]["q"], 110.0, 1e-12)
    close(sim.lots[0]["sell_q"], 1.0, 1e-12)
    sim.lots[0]["sold"] = True                     # once a monthly lot has sold, new buys start a new lot
    sim.add_trade(25, 1.0, 100.0)
    assert len(sim.lots) == 2


def test_stress_bull_mode_keeps_a_runner():
    rows, px = [], 50.0
    for _ in range(300):
        rows.append((px, px * 1.001, px * 0.999, px * 1.003))
        px *= 1.003
    s = st_series(rows)
    assert s.bull(298)
    v = dict(stress.BASE, bull=True)
    sim = stress.Sim(s, 290, 299, v)
    sim.lots = [{"q": 0.5, "cost": 0.5 * 80.0, "sell_q": 0.5, "done": [True, False], "runner": False, "month": s.ym[290], "sold": True}]
    sim.day(299)                                   # high is far above 80 x 1.4
    lot = sim.lots[0]
    close(lot["q"], 0.25, 1e-12)
    assert lot["runner"]
    assert [o for o in sim.snapshot() if o[0] == "runner"][0][2] == 160.0
    v2 = dict(stress.BASE, bull=False)              # without bull mode the whole remainder sells
    sim2 = stress.Sim(s, 290, 299, v2)
    sim2.lots = [{"q": 0.5, "cost": 40.0, "sell_q": 0.5, "done": [True, False], "runner": False, "month": s.ym[290], "sold": True}]
    sim2.day(299)
    assert sim2.lots == []


def test_stress_new_buy_backs_wait_a_day():
    s = st_series([(100, 100, 100, 100)] * 5 + [(100, 150, 50, 100)])
    sim = stress.Sim(s, 0, 5, stress.BASE)
    sim.lots = [{"q": 1.0, "cost": 100.0, "sell_q": 0.5, "done": [False, False], "runner": False, "month": s.ym[0], "sold": False}]
    sim.day(5)                                     # +20% and +40% fill; the low would hit the buy-backs too
    assert sim.sells == 2 and sim.buys == 0
    assert len(sim.sales) == 2 and not any(lv["filled"] for x in sim.sales for lv in x["levels"])


def test_stress_stale_cash_goes_back_to_buy_ins():
    s = st_series([(100, 100, 100, 100)] * 200)
    sim = stress.Sim(s, 0, 199, dict(stress.BASE, stale=True))
    sim.rungs = {"base": 100, "done": [False] * 4, "frozen": False}
    sim.sales = [{"q": 1, "rebought": 0, "swept": 0, "day": 50, "price": 90.0,
                  "levels": [{"price": 72, "usd": 45, "filled": False}, {"price": 54, "usd": 45, "filled": False}]}]
    sim.end_of_day(169)                            # 119 days: too soon
    assert sim.sales and sim.waiting() == 0
    sim.end_of_day(170)                            # 120 days and price (100) above the sale (90)
    assert sim.sales == [] and sim.waiting() == 90
    sim2 = stress.Sim(s, 0, 199, dict(stress.BASE, stale=False))
    sim2.rungs = sim.rungs
    sim2.sales = [{"q": 1, "rebought": 0, "swept": 0, "day": 50, "price": 90.0,
                   "levels": [{"price": 72, "usd": 45, "filled": False}]}]
    sim2.end_of_day(170)
    assert sim2.sales                               # rule off: money keeps waiting


def test_stress_canadian_average_cost_tax():
    s = st_series([(100, 100, 100, 100)] * 800)
    sim = stress.Sim(s, 0, 799, stress.BASE)
    sim.acb_buy(100.0, 1.0)
    sim.acb_buy(200.0, 1.0)
    sim.acb_sell(10, 300.0, 1.0)                   # average cost 150 -> gain 150 (2025)
    close(sim.gains[2025], 150.0, 1e-9)
    close(sim.acb_cost, 150.0, 1e-9)
    sim.acb_sell(400, 50.0, 0.5)                   # 2026: 0.5 coin for $50 vs $75 cost = loss 25 ...
    sim.gains[2027] = 100.0                        # ... carried into 2027: taxed on 75
    r = sim.result()
    close(r["tax"], (150 + 75) * 0.5 * stress.MARGINAL_TAX, 1e-9)


def test_stress_cash_matched_benchmark():
    close(stress.cash_matched({"coins": 10.0, "contributed": 1000.0}, 0.2, 100.0), 10.0, 1e-12)
    close(stress.cash_matched({"coins": 10.0, "contributed": 1000.0}, 0.2, 50.0), 12.0, 1e-12)


def test_stress_money_and_coins_add_up():
    rng = random.Random(3)
    rows, px = [], 100.0
    for _ in range(1000):
        o = px
        px *= math.exp(rng.gauss(0.0005, 0.05))
        rows.append((o, max(o, px) * 1.03, min(o, px) * 0.97, px))
    s = st_series(rows)
    for v in (stress.BASE, dict(stress.BASE, lots="month", core=0.5), dict(stress.BASE, lots="avg"),
              dict(stress.BASE, bull=True, stale=True, core=0.7)):
        sim = stress.Sim(s, 210, 940, v, record=True)
        r = sim.run()
        close(r["contributed"] - sum(e[3] for e in sim.events), r["waiting"], 0.01 + 0.005 * len(sim.events))
        close(sum(e[4] for e in sim.events), r["coins"], 1e-6)
        assert r["core"] >= 0 and r["trade"] >= -1e-12 and r["waiting"] >= -0.01
        assert max(sim.month_buyins.values()) <= 3


def test_history_parsers_and_cleaning():
    cb = history.parse_coinbase([[1496534400, 224, 249.04, 224.95, 245.5, 212990.77]])
    assert cb == [{"t": 1496534400, "o": 224.95, "h": 249.04, "l": 224.0, "c": 245.5, "v": 212990.77}]
    bs = history.parse_bitstamp({"data": {"pair": "XRP/USD", "ohlc": [
        {"timestamp": "1500000000", "open": "0.2", "high": "0.25", "low": "0.19", "close": "0.22", "volume": "1000"}]}})
    assert bs[0]["o"] == 0.2 and bs[0]["c"] == 0.22 and bs[0]["t"] == 1500000000
    D = 86400
    good = lambda t, p: {"t": t, "o": p, "h": p * 1.1, "l": p * 0.9, "c": p, "v": 1}
    rows = [good(0, 1)] + [good(20 * D + k * D, 2) for k in range(3)] + [good(26 * D, 3), good(26 * D, 3)]
    rows.append({"t": 27 * D, "o": 3, "h": 3.3, "l": 3.2, "c": 3, "v": 1})     # low above the open: rejected
    rows.append(good(28 * D, 4))
    rows.append(good(29 * D, 9))                                                # unfinished day: dropped
    cs, gaps = history.clean(rows, now=29 * D + 100)
    assert len(gaps) == 1                                                       # the 20-day hole: older part dropped
    assert cs[0]["t"] == 20 * D and cs[-1]["t"] == 28 * D
    assert [c["t"] // D for c in cs] == list(range(20, 29))                     # short holes filled day by day
    assert cs[3]["c"] == 2 and cs[3]["v"] == 0.0                                # a filled day repeats the last close


# =================================================================== your plan, replayed (myplan.py)
def test_myplan_flat_price_matches_plain_buying():
    s = st_series([(100, 100, 100, 100)] * 120)                       # Jan 1 .. Apr 30: 8 buy days
    r = myplan.replay(s.d, amount=10.0)
    assert r["buy_days"] == 8 and r["invested"] == 80.0 and r["sells"] == 0
    close(r["coins"], r["dca_coins"], 1e-12)                          # nothing sold: same coins as plain buying
    close(r["core"], 8 * 7 * (1 - myplan.TAKER) / 100, 1e-12)        # 70% to Core
    close(r["value"], r["coins"] * 100, 0.01)


def test_myplan_sell_then_monthly_rollover():
    rows = [(100, 100, 100, 100)] * 40 + [(125, 130, 124, 125)] + [(125, 126, 124, 125)] * 80
    s = st_series(rows)                                               # Jan 1 .. May 1
    r = myplan.replay(s.d, amount=10.0)
    assert r["sells"] >= 1                                            # the January lot took profit at +20%
    assert r["rolled_over_cash"] > 0                                  # its buy-backs never filled -> rolled on Mar 1
    assert r["cash"] == 0                                             # ... and was spent over the next 3 buy days
    assert r["invested"] == 10.0 * r["buy_days"]
    assert r["coins"] < r["dca_coins"]                                # sold at +20% and bought back higher: fewer coins


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
                eng = c["engine"]
                for o in c["orders"]:
                    if o["kind"] == "dip_buy":
                        close(o["price"], eng["anchor"] * (1 - o["drop"] / 100), 1e-9)
                    if o["kind"] == "take_profit":
                        close(o["price"], eng["ref"] * (1 + o["pct"] / 100), 1e-9)
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
