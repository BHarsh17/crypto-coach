#!/usr/bin/env python3
"""Crypto Coach backtest: replay the app's rules on real Kraken prices.

For each coin it downloads Kraken's daily and weekly candles, then walks day by
day through the test window. Each day it asks the watcher's own score_coin()
for the signals (so the maths is identical to the live app) and applies the
app's rules: ladder rungs, buy-early, trims, trailing stop.

Strategies compared, all starting with the same coins + the same cash reserve:
  v4           the live v4 rules (take-profits +20/+40%, buy-backs -20/-40% below each sale,
               deep dip ladder), run by the same engine as the watcher
  app          the v3 rules (flag trims, ladder, buy-early)
  ladder_only  the same but without the buy-early rule
  reanchor     ladder only, and after every trim the ladder moves to the trim price
  hybrid       half the reserve on a monthly schedule, half on a deeper ladder
               (-10/-25/-40/-55%), ladder moves after trims, no buy-early
Score: coin_equiv = coins + cash converted at the final price (total value in coins).
  dca          the reserve split into equal monthly buys
  lump         the whole reserve bought on day 1
  cash         keep the reserve as cash

Not testable historically (no free history): news, FOMO, leverage/funding,
shorts-crowded. Those signals are treated as OFF.

Usage: python3 backtest/backtest.py [--months 18] [--extra XBTUSD:BTC] [--out site/backtest.json]
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "watcher"))
import engine  # noqa: E402
import watch  # noqa: E402

DAY = 86400
FEE = 0.0025            # Kraken Pro maker fee (limit orders), both ways
START_HOLDING_USD = 1000.0
RESERVE_USD = 500.0
CORE_SHARE = 0.4        # 40% core, 60% trade bucket (like keeping 4 of 10 coins)


def day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


# ----------------------------------------------------------------- signals (same maths as the live watcher)
def make_signal_fn(daily, weekly, th, ladder):
    cache = {}

    def signals(i):
        if i in cache:
            return cache[i]
        d = daily[: i + 1]
        end = d[-1]["t"] + DAY
        wk = [dict(w) for w in weekly if w["t"] + 7 * DAY <= end]
        cur = [w["t"] for w in weekly if w["t"] < end < w["t"] + 7 * DAY]
        start = cur[0] if cur else (wk[-1]["t"] + 7 * DAY if wk else d[-1]["t"])
        px = d[-1]["c"]
        wk.append({"t": start, "o": px, "h": px, "l": px, "c": px, "v": 0.0})   # the live, unfinished week
        series = {1440: d, 10080: wk, 240: d[-40:], 60: d[-40:]}
        orig = (watch.candles, watch.futures_info)
        watch.candles = lambda pair, interval: series[interval]
        watch.futures_info = lambda sym: None
        try:
            s = watch.score_coin({"symbol": "X", "kraken": "X", "futures": "", "anchor": 1.0}, th, ladder, None)
        finally:
            watch.candles, watch.futures_info = orig
        out = ([k for k, f in s["flags"].items() if f["on"]], [k for k, b in s["bottoms"].items() if b["on"]])
        cache[i] = out
        return out
    return signals


# ----------------------------------------------------------------- one strategy run
def simulate(daily, i0, i1, signals, ladder, th, buy_early=True, boost_fraction=1.0,
             reanchor_on_trim=False, dca_share=0.0):
    """dca_share: part of the reserve bought on a monthly schedule instead of via the ladder.
    reanchor_on_trim: after a trim, rebuild the ladder from the trim price using the cash on hand."""
    p0 = daily[i0]["c"]
    held = START_HOLDING_USD / p0
    st = {"core": held * CORE_SHARE, "trade": held * (1 - CORE_SHARE), "cash": RESERVE_USD,
          "reserve_total": RESERVE_USD, "fees": 0.0}
    lad = {"anchor": p0, "budget": RESERVE_USD * (1 - dca_share), "left": {n: 1.0 for n in range(1, len(ladder) + 1)}}
    dca_pool = RESERVE_USD * dca_share
    n_dca = max(1, round((i1 - i0) / 30.4375)) if dca_share > 0 else 0
    dca_days = {i0 + int(k * (i1 - i0 + 1) / n_dca) for k in range(n_dca)} if n_dca else set()
    boost_done = 0
    cyc = {"baseline": None, "tiers_done": 0, "peak": None}
    events, trims = [], 0

    def rung_px(n):
        return lad["anchor"] * (1 - ladder[n - 1]["drop"] / 100)

    def rung_usd(n):
        base = lad["budget"] if (reanchor_on_trim or dca_share) else st["reserve_total"]
        return base * ladder[n - 1]["share"] / 100

    def buy(usd, px, why, t, pool_cap=None):
        usd = min(usd, st["cash"] if pool_cap is None else min(st["cash"], pool_cap))
        if usd <= 0.005:
            return 0.0
        q = usd * (1 - FEE) / px
        st["cash"] -= usd
        st["fees"] += usd * FEE
        st["trade"] += q
        events.append([day(t), why, round(px, 6), round(usd, 2), round(q, 6)])
        return usd

    def sell(q, px, why, t):
        q = min(q, st["trade"])
        if q <= 0:
            return
        got = q * px * (1 - FEE)
        st["trade"] -= q
        st["cash"] += got
        st["reserve_total"] += got        # trims refill the dip reserve
        st["fees"] += q * px * FEE
        events.append([day(t), why, round(px, 6), round(-got, 2), round(-q, 6)])
        if reanchor_on_trim:              # the ladder moves to the sale price, funded by the cash on hand
            lad["anchor"] = px
            lad["budget"] = st["cash"] - (dca_pool if dca_share else 0)
            lad["left"] = {n: 1.0 for n in lad["left"]}

    for i in range(i0, i1 + 1):
        c = daily[i]
        if i in dca_days:
            dca_pool -= buy(RESERVE_USD * dca_share / n_dca, c["c"], "scheduled monthly buy", c["t"])
        if i > i0:   # resting limit orders fill when the day's low touches them
            for n in lad["left"]:
                if lad["left"][n] > 1e-9 and c["l"] <= rung_px(n):
                    cap = st["cash"] - max(dca_pool, 0) if dca_share else None
                    buy(rung_usd(n) * lad["left"][n], rung_px(n), f"ladder rung {n} filled", c["t"], cap)
                    lad["left"][n] = 0.0
                    cyc = {"baseline": None, "tiers_done": 0, "peak": None}
        flags, bots = signals(i)
        px = c["c"]
        if cyc["tiers_done"] >= 1 and st["trade"] > 0:
            cyc["peak"] = max(cyc["peak"], c["h"])
            if px <= cyc["peak"] * (1 - th["trailing_stop_pct"] / 100):
                sell(st["trade"], px, "trailing stop", c["t"])
                cyc = {"baseline": None, "tiers_done": 0, "peak": None}
        t_ = watch.tier(len(flags))
        if len(flags) >= 3 and t_ > cyc["tiers_done"] and st["trade"] > 0:
            if cyc["baseline"] is None:
                cyc["baseline"], cyc["peak"] = st["trade"], px
            q = min(st["trade"], cyc["baseline"] * 0.25 * (t_ - cyc["tiers_done"]))
            tiers = cyc["tiers_done"]
            sell(q, px, f"trim on {len(flags)} top flags ({', '.join(flags)})", c["t"])
            cyc["tiers_done"] = max(t_, tiers)
            trims += 1
        bt = watch.tier(len(bots))
        if buy_early and len(bots) >= 3 and bt > boost_done:
            fresh = [n for n in sorted(lad["left"]) if lad["left"][n] >= 1.0 - 1e-9][: bt - boost_done]
            if fresh:
                usd = sum(rung_usd(n) * boost_fraction for n in fresh)
                buy(usd, px, f"buy early (rung {'+'.join(map(str, fresh))}) on {len(bots)} bottom signals ({', '.join(bots)})", c["t"])
                for n in fresh:
                    lad["left"][n] -= boost_fraction
                cyc = {"baseline": None, "tiers_done": 0, "peak": None}
            boost_done = bt
    return finish(st, daily[i1]["c"], p0, events, trims)


def simulate_tp(daily, i0, i1, signals, th, tp_levels, tp_share, rebuy, dip, flags_accelerate=False):
    """Rise ladder (take-profits) + dip ladder.
    tp_levels: % above your average cost where a slice of the trade bucket is sold (limit orders: fill if
               the day's high reaches them). tp_share: slice size as a share of the trade bucket at the
               start of the cycle. After every sale, the buy-back ladder `rebuy` (drops below the SALE
               price) is rebuilt with the cash on hand. A buy-back starts a fresh cycle.
    flags_accelerate: 3+ top flags sell the next unfilled level right away at the close."""
    p0 = daily[i0]["c"]
    held = START_HOLDING_USD / p0
    st = {"core": held * CORE_SHARE, "trade": held * (1 - CORE_SHARE), "cash": RESERVE_USD,
          "reserve_total": RESERVE_USD, "fees": 0.0, "cost_total": START_HOLDING_USD}
    lad = {"anchor": p0, "budget": RESERVE_USD, "rungs": dip, "left": [1.0] * len(dip), "day": i0}
    cyc = {"baseline": None, "done": set(), "accel": 0, "day": i0}
    # An order placed today can't fill until tomorrow: we can't know whether the day's high came before its low.
    events, sells = [], 0

    def coins():
        return st["core"] + st["trade"]

    def avg_cost():
        return st["cost_total"] / coins() if coins() > 0 else p0

    def buy(usd, px, why, t):
        usd = min(usd, st["cash"])
        if usd <= 0.005:
            return
        q = usd * (1 - FEE) / px
        st["cash"] -= usd
        st["fees"] += usd * FEE
        st["trade"] += q
        st["cost_total"] += usd
        events.append([day(t), why, round(px, 6), round(usd, 2), round(q, 6)])

    def sell(q, px, why, t):
        nonlocal sells
        q = min(q, st["trade"])
        if q <= 1e-12:
            return
        ac = avg_cost()
        got = q * px * (1 - FEE)
        st["trade"] -= q
        st["cost_total"] -= q * ac          # average-cost accounting: selling doesn't change the average
        st["cash"] += got
        st["fees"] += q * px * FEE
        sells += 1
        events.append([day(t), why, round(px, 6), round(-got, 2), round(-q, 6)])
        lad.update(anchor=px, budget=st["cash"], rungs=rebuy, left=[1.0] * len(rebuy), day=today[0])

    today = [i0]
    for i in range(i0, i1 + 1):
        c = daily[i]
        today[0] = i
        if i > i0 and lad["day"] < i:
            for k, r in enumerate(lad["rungs"]):
                px = lad["anchor"] * (1 - r["drop"] / 100)
                if lad["left"][k] > 1e-9 and c["l"] <= px:
                    buy(lad["budget"] * r["share"] / 100 * lad["left"][k], px, f"buy-back rung {k + 1} filled", c["t"])
                    lad["left"][k] = 0.0
                    cyc = {"baseline": None, "done": set(), "accel": 0, "day": i}
        if i > i0 and cyc["day"] < i:
            for k, lvl in enumerate(tp_levels):
                if k in cyc["done"] or st["trade"] <= 1e-12:
                    continue
                target = avg_cost() * (1 + lvl / 100)
                if c["h"] >= target:
                    if cyc["baseline"] is None:
                        cyc["baseline"] = st["trade"]
                    sell(cyc["baseline"] * tp_share, target, f"take-profit +{lvl}% hit", c["t"])
                    cyc["done"].add(k)
        if flags_accelerate:
            flags, _ = signals(i)
            t_ = watch.tier(len(flags))
            if len(flags) >= 3 and t_ > cyc["accel"] and st["trade"] > 1e-12:
                nxt = [k for k in range(len(tp_levels)) if k not in cyc["done"]][: t_ - cyc["accel"]]
                for k in nxt:
                    if cyc["baseline"] is None:
                        cyc["baseline"] = st["trade"]
                    sell(cyc["baseline"] * tp_share, c["c"], f"overheated ({len(flags)} flags): next take-profit early", c["t"])
                    cyc["done"].add(k)
                cyc["accel"] = t_
    return finish(st, daily[i1]["c"], p0, events, sells)


def simulate_v4(daily, i0, i1, rules=None):
    """The live v4 rules, driven by the SAME engine the watcher uses."""
    rules = rules or engine.DEFAULT_RULES
    p0 = daily[i0]["c"]
    held = START_HOLDING_USD / p0
    st = {"core": held * CORE_SHARE, "trade": held * (1 - CORE_SHARE), "cash": RESERVE_USD, "fees": 0.0}
    eng = engine.new_state(p0, p0, rules)
    proceeds, events, sells = {}, [], 0
    base = {"cycle": None, "trade": 0.0}
    for i in range(i0 + 1, i1 + 1):
        c = daily[i]
        for e in engine.step(eng, c["h"], c["l"], c["t"], rules):
            px = e["price"]
            if e["kind"] == "take_profit":
                if base["cycle"] != e["cycle"]:
                    base.update(cycle=e["cycle"], trade=st["trade"])
                q = min(st["trade"], base["trade"] * e["share"] / 100)
                if q <= 1e-12:
                    continue
                got = q * px * (1 - FEE)
                st["trade"] -= q
                st["cash"] += got
                st["fees"] += q * px * FEE
                proceeds[e["id"]] = got
                sells += 1
                events.append([day(c["t"]), f"JUMP OUT: take-profit +{e['pct']}%", round(px, 6), round(-got, 2), round(-q, 6)])
            else:
                usd = (proceeds.get(e["sale_id"], 0.0) * e["share"] / 100 if e["kind"] == "buy_back"
                       else RESERVE_USD * e["share"] / 100)
                usd = min(usd, st["cash"])
                if usd <= 0.005:
                    continue
                q = usd * (1 - FEE) / px
                st["cash"] -= usd
                st["trade"] += q
                st["fees"] += usd * FEE
                why = "JUMP IN: buy-back" if e["kind"] == "buy_back" else f"JUMP IN: dip rung {e['rung']}"
                events.append([day(c["t"]), why, round(px, 6), round(usd, 2), round(q, 6)])
    return finish(st, daily[i1]["c"], p0, events, sells)


def finish(st, end_px, p0, events, trims=0):
    coins = st["core"] + st["trade"]
    start_coins = START_HOLDING_USD / p0
    bought = coins - start_coins
    spent = sum(e[3] for e in events if e[3] > 0)
    return {"coins": round(coins, 6), "cash": round(st["cash"], 2), "value": round(coins * end_px + st["cash"], 2),
            "coin_equiv": round(coins + st["cash"] / end_px, 6),
            "extra_coins": round(bought, 6), "fees": round(st["fees"], 2), "trims": trims,
            "avg_buy_price": round(spent / sum(e[4] for e in events if e[4] > 0), 6) if spent else None,
            "events": events}


def simple(daily, i0, i1, kind):
    p0 = daily[i0]["c"]
    st = {"core": START_HOLDING_USD / p0, "trade": 0.0, "cash": RESERVE_USD, "fees": 0.0}
    events = []
    if kind in ("dca", "lump"):
        n = 1 if kind == "lump" else max(1, round((i1 - i0) / 30.4375))
        idx = [i0 + int(k * (i1 - i0 + 1) / n) for k in range(n)]
        for i in idx:
            usd = RESERVE_USD / n
            q = usd * (1 - FEE) / daily[i]["c"]
            st["cash"] -= usd
            st["fees"] += usd * FEE
            st["trade"] += q
            events.append([day(daily[i]["t"]), "scheduled buy" if kind == "dca" else "buy everything", daily[i]["c"], round(usd, 2), round(q, 6)])
    st["cash"] = round(st["cash"], 8)
    return finish(st, daily[i1]["c"], p0, events)


# ----------------------------------------------------------------- data
def fetch(pair):
    d = watch.candles(pair, 1440)
    time.sleep(1.2)
    w = watch.candles(pair, 10080)
    time.sleep(1.2)
    return d, w


def half2(step):
    """Your manual method's buy-backs: half at -step%, half at -2*step% below the sale price."""
    return [{"drop": step, "share": 50}, {"drop": 2 * step, "share": 50}]


def overall(coins):
    """Rank strategies across coins by how they did versus monthly DCA (total value in coins)."""
    rows = {}
    ok = [c for c in coins if "results" in c]
    for c in ok:
        dca = c["results"]["dca"]["coin_equiv"]
        for k, r in c["results"].items():
            rows.setdefault(k, []).append(r["coin_equiv"] / dca - 1)
    out = [{"strategy": k, "avg_vs_dca_pct": round(100 * sum(v) / len(v), 2),
            "worst_vs_dca_pct": round(100 * min(v), 2),
            "coins_beating_dca": sum(1 for x in v if x > 1e-9), "coins": len(v)} for k, v in rows.items()]
    return sorted(out, key=lambda r: (-r["coins_beating_dca"], -r["avg_vs_dca_pct"]))


def backtest_coin(symbol, daily, weekly, months, cfg, now=None):
    th, ladder = cfg["thresholds"], cfg["ladder"]
    now = time.time() if now is None else now
    i1 = max(i for i, c in enumerate(daily) if c["t"] + DAY <= now)   # last finished day
    i0 = max(60, i1 - int(round(months * 30.4375)))            # keep 60+ days of warm-up history
    sig = make_signal_fn(daily, weekly, th, ladder)
    deep = [{"drop": 10, "share": 25}, {"drop": 25, "share": 25}, {"drop": 40, "share": 25}, {"drop": 55, "share": 25}]
    res = {
        "v4": simulate_v4(daily, i0, i1, engine.rules_from(cfg.get("rules"))),
        "app": simulate(daily, i0, i1, sig, ladder, th),
        "ladder_only": simulate(daily, i0, i1, sig, ladder, th, buy_early=False),
        "reanchor": simulate(daily, i0, i1, sig, ladder, th, buy_early=False, reanchor_on_trim=True),
        "hybrid": simulate(daily, i0, i1, sig, deep, th, buy_early=False, reanchor_on_trim=True, dca_share=0.5),
        "rise": simulate_tp(daily, i0, i1, sig, th, [25, 50, 75, 100], 0.25, deep, deep),
        "rise_flags": simulate_tp(daily, i0, i1, sig, th, [25, 50, 75, 100], 0.25, deep, deep, flags_accelerate=True),
        "yours_10_20": simulate_tp(daily, i0, i1, sig, th, [10, 20], 0.5, half2(10), deep),
        "yours_20_40": simulate_tp(daily, i0, i1, sig, th, [20, 40], 0.5, half2(20), deep),
        "yours_30_60": simulate_tp(daily, i0, i1, sig, th, [30, 60], 0.5, half2(30), deep),
        "dca": simple(daily, i0, i1, "dca"),
        "lump": simple(daily, i0, i1, "lump"),
        "cash": simple(daily, i0, i1, "cash"),
    }
    days_f = {}
    days_b = {}
    maxf = maxb = d3f = d3b = 0
    for i in range(i0, i1 + 1):
        f, b = sig(i)
        maxf, maxb = max(maxf, len(f)), max(maxb, len(b))
        d3f += len(f) >= 3
        d3b += len(b) >= 3
        for k in f:
            days_f[k] = days_f.get(k, 0) + 1
        for k in b:
            days_b[k] = days_b.get(k, 0) + 1
    win = daily[i0:i1 + 1]
    return {"symbol": symbol, "start": day(daily[i0]["t"]), "end": day(daily[i1]["t"]), "days": i1 - i0 + 1,
            "start_price": daily[i0]["c"], "end_price": daily[i1]["c"],
            "low": min(c["l"] for c in win), "high": max(c["h"] for c in win),
            "change_pct": round((daily[i1]["c"] / daily[i0]["c"] - 1) * 100, 2),
            "signals": {"max_top_flags": maxf, "max_bottom_signals": maxb, "days_3plus_top": d3f,
                        "days_3plus_bottom": d3b, "top_flag_days": days_f, "bottom_signal_days": days_b},
            "results": res}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=os.path.join(HERE, "..", "coins.json"))
    ap.add_argument("--months", type=float, default=18)
    ap.add_argument("--extra", action="append", default=["XBTUSD:BTC"], help="extra Kraken pair:SYMBOL to include")
    ap.add_argument("--out", default="site/backtest.json")
    ap.add_argument("--reuse", default=os.environ.get("PREV_BACKTEST_URL", ""),
                    help="URL of the last published backtest; reused if under 20 hours old, same version and same coins")
    a = ap.parse_args()
    cfg = json.load(open(a.config))
    if a.reuse:
        try:
            prev = watch.get_json(a.reuse + "?t=" + str(int(time.time())))
            age = time.time() - datetime.fromisoformat(prev["generated_at"]).timestamp()
            want = sorted({c["symbol"] for c in cfg["coins"]} | {e.split(":")[1] for e in a.extra})
            have = sorted(c["symbol"] for c in prev.get("coins", []))
            if age < 20 * 3600 and prev.get("version") == watch.VERSION and prev.get("months") == a.months and want == have:
                os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
                with open(a.out, "w") as f:
                    json.dump(prev, f)
                print(f"reused backtest from {prev['generated_at']}")
                return
        except Exception as e:  # noqa: BLE001
            print("no reusable backtest:", e)
    pairs = [(c["kraken"], c["symbol"]) for c in cfg["coins"]]
    for e in a.extra:
        p, s = e.split(":")
        if s not in [x[1] for x in pairs]:
            pairs.append((p, s))
    out = []
    for pair, sym in pairs:
        try:
            d, w = fetch(pair)
            out.append(backtest_coin(sym, d, w, a.months, cfg))
            print(f"{sym}: done")
        except Exception as e:  # noqa: BLE001
            print(f"{sym} failed: {e}", file=sys.stderr)
            out.append({"symbol": sym, "error": str(e)[:200]})
    doc = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "version": watch.VERSION,
           "months": a.months, "assumptions": {
               "start": f"${START_HOLDING_USD:,.0f} of the coin ({CORE_SHARE:.0%} core, {1 - CORE_SHARE:.0%} trade) + ${RESERVE_USD:,.0f} dip reserve",
               "fees": f"{FEE * 100:.2f}% per trade (Kraken Pro limit orders)",
               "fills": "ladder rungs fill at the rung price if the day's low touches it; other actions at the daily close",
               "not_tested": "news, FOMO, leverage and shorts-crowded signals (no free history), so they are treated as off"},
           "coins": out, "overall": overall(out)}
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(doc, f, indent=1)
    for c in out:
        if "results" in c:
            r = c["results"]
            print(f"{c['symbol']:4s} {c['change_pct']:+7.1f}%  " + "  ".join(f"{k}={v['coin_equiv']:.6g}" for k, v in r.items()))


if __name__ == "__main__":
    main()
