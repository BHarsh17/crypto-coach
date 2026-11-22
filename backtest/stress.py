#!/usr/bin/env python3
"""Crypto Coach stress test: the v5 rules on up to 10 years of daily prices.

Why this exists (from three independent reviews of the strategy):
  - the 22-month Kraken backtest is one mostly-falling stretch -> use history back to 2016/17,
    in rolling 24-month windows, and split results by bull / bear / sideways windows
  - "coins + cash" flatters anything that holds cash in a falling market -> compare against
    DCA that holds the SAME share of cash ("cash-matched DCA"), and against random buy timing
  - costs and tax -> taker fee on market buys, fills at the gap price, a Canadian
    average-cost (ACB) capital-gains estimate
  - 1,000-path style check -> block bootstrap of real price chunks

Investor setup tested: $500 to start, $100 on the 1st of each month after that, at most
3 buy-ins a month (buy-backs and sells are resting limit orders and don't count).

Rules (v5 draft, see STRATEGY.md), each coin on its own:
  buy-in ladder   rungs -10/-25/-40/-55% below the highest high of the last LOOKBACK days
                  (trails up until a rung fills, then freezes; resets when all 4 filled or the
                  price closes above it). A rung uses cash / (rungs not filled yet).
                  Rungs already passed when the ladder (re)arms buy at market, at most one per week.
                  Money no rung used within 60 days is bought anyway at market.
  split           CORE share to Core (never sold), the rest to the Trade bucket as a lot
  lots            "buy": every buy its own lot | "month": a calendar month's buys form one lot
                  (until that lot first sells) | "avg": one lot = one average cost
  sell ladder     per lot: half at +20%, the rest at +40%
  bull mode       if the price is above a rising 200-day average, the +40% sell keeps half the
                  remainder as a runner: sold at +100%, or at market when the close drops below
                  the 50-day average
  buy-back ladder per sale: -20% / -40%, half the sale money each; coins bought back beyond
                  those sold move to Core
  stale cash      buy-back money unused for 120 days while the price is above the sale price
                  goes back into the buy-in ladder's cash
Fills: a resting limit buy fills at min(order, open) if the low reaches it; a limit sell at
max(order, open) if the high reaches it. Orders created during a day can't fill that day.
Market orders (passed rungs, 60-day buys, runner exits, DCA) fill at the next open.
"""
import argparse
import json
import math
import os
import random
import sys
import time
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "..", "watcher"))

DAY = 86400
MAKER = 0.0025           # Kraken Pro maker (resting limit orders)
TAKER = 0.0040           # Kraken Pro taker (market orders)
MIN_ORDER = 5.0
START_USD = 500.0
MONTHLY_USD = 100.0
MAX_BUYINS_MONTH = 3
PASSED_RUNG_GAP_DAYS = 7
MAX_WAIT_DAYS = 60
STALE_DAYS = 120
WINDOW_DAYS = 730
WINDOW_STEP = 91
WARMUP = 200
MARGINAL_TAX = 0.30      # assumed combined BC + federal marginal rate; 50% of a capital gain is taxed
DIP = (10, 25, 40, 55)
BASE = {"lots": "buy", "core": 0.4, "bull": False, "stale": False, "lookback": 30, "tp": (20, 40), "bb": (20, 40)}


def ymd(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def ym(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m")


def year(t):
    return datetime.fromtimestamp(t, timezone.utc).year


def averages(d, n):
    out, s = [], 0.0
    for i, c in enumerate(d):
        s += c["c"]
        if i >= n:
            s -= d[i - n]["c"]
        out.append(s / min(i + 1, n))
    return out


class Series:
    def __init__(self, daily):
        self.d = daily
        self.ma200 = averages(daily, 200)
        self.ma50 = averages(daily, 50)
        self.ym = [ym(c["t"]) for c in daily]
        self.yr = [year(c["t"]) for c in daily]

    def bull(self, i):
        return i >= 220 and self.d[i]["c"] > self.ma200[i] > self.ma200[i - 20]

    def high(self, i, n):
        return max(c["h"] for c in self.d[max(0, i - n + 1): i + 1])


def vname(v):
    return f"lots={v['lots']} core={int(v['core'] * 100)} bull={'on' if v['bull'] else 'off'} stale={'on' if v['stale'] else 'off'}"


class Sim:
    def __init__(self, s, i0, i1, v, entry="ladder", p_random=0.0, seed=0, record=False):
        self.s, self.i0, self.i1, self.v = s, i0, i1, v
        self.entry, self.p_random, self.rng, self.record = entry, p_random, random.Random(seed), record
        self.cash = []                      # [[day_index, usd]]
        self.core = 0.0
        self.lots = []                      # {"q", "cost", "sell_q", "done": [..], "runner", "month", "sold"}
        self.sales = []                     # {"q", "rebought", "swept", "day", "price", "levels": [{"price", "usd", "filled"}]}
        self.fees = self.contributed = 0.0
        self.buys = self.sells = self.buyins = 0
        self.month_buyins = {}
        self.acb_cost = self.acb_q = 0.0
        self.gains = {}                     # year -> realized capital gain (CRA average cost)
        self.cash_share_sum = 0.0
        self.events = []
        self.rungs = None
        self.passed = []                    # rung indexes waiting to be bought at market
        self.last_market_rung = -10 ** 9
        self.pending = []                   # market actions at the next open: ("buy", usd, why, is_buyin) / ("runner_exit",)

    # ---------------------------------------------------------------- money helpers
    def waiting(self):
        return sum(u for _, u in self.cash)

    def reserved(self):
        return sum(lv["usd"] for s in self.sales for lv in s["levels"] if not lv["filled"])

    def take_cash(self, usd):
        left = usd
        while left > 1e-9 and self.cash:
            t = min(left, self.cash[0][1])
            self.cash[0][1] -= t
            left -= t
            if self.cash[0][1] <= 1e-9:
                self.cash.pop(0)

    def log(self, i, what, px, usd, q):
        if self.record:
            self.events.append([ymd(self.s.d[i]["t"]), what, round(px, 6), round(usd, 2), round(q, 8)])

    def cap_ok(self, i):
        queued = sum(1 for a in self.pending if a[0] == "buy")
        return self.month_buyins.get(self.s.ym[i], 0) + queued < MAX_BUYINS_MONTH

    def count_buyin(self, i):
        k = self.s.ym[i]
        self.month_buyins[k] = self.month_buyins.get(k, 0) + 1
        self.buyins += 1

    def acb_buy(self, usd, q):
        self.acb_cost += usd
        self.acb_q += q

    def acb_sell(self, i, got, q):
        avg = self.acb_cost / self.acb_q if self.acb_q > 0 else 0.0
        y = self.s.yr[i]
        self.gains[y] = self.gains.get(y, 0.0) + got - avg * q
        self.acb_cost -= avg * q
        self.acb_q = max(0.0, self.acb_q - q)

    # ---------------------------------------------------------------- lots
    def add_trade(self, i, q, usd):
        if q <= 1e-15:
            return
        mode, month = self.v["lots"], self.s.ym[i]
        target = None
        if mode == "avg" and self.lots:
            target = self.lots[0]
        elif mode == "month" and self.lots and self.lots[-1]["month"] == month and not self.lots[-1]["sold"]:
            target = self.lots[-1]
        if target is None:
            self.lots.append({"q": q, "cost": usd, "sell_q": q / 2, "done": [False, False], "runner": False,
                              "month": month, "sold": False})
        else:
            target["q"] += q
            target["cost"] += usd
            target["sell_q"] = target["q"] / 2
            target["done"] = [False, False]
            target["runner"] = False

    def buy_in(self, i, usd, px, fee, why):
        usd = min(usd, self.waiting())
        if usd < MIN_ORDER:
            return False
        q = usd * (1 - fee) / px
        self.take_cash(usd)
        self.fees += usd * fee
        self.acb_buy(usd, q)
        core = q * self.v["core"]
        self.core += core
        self.add_trade(i, q - core, usd * (1 - self.v["core"]))
        self.buys += 1
        self.count_buyin(i)
        self.log(i, why, px, usd, q)
        return True

    def sell(self, i, lot, q, px, fee, why):
        if q * px < MIN_ORDER:
            if lot["q"] * px < MIN_ORDER:
                return
            q = lot["q"]
        q = min(q, lot["q"])
        per = lot["cost"] / lot["q"]
        got = q * px * (1 - fee)
        lot["q"] -= q
        lot["cost"] -= per * q
        lot["sold"] = True
        self.fees += q * px * fee
        self.sells += 1
        self.acb_sell(i, got, q)
        bb = self.v["bb"]
        if got / len(bb) < MIN_ORDER:
            levels = [{"price": px * (1 - bb[0] / 100), "usd": got, "filled": False}]
        else:
            levels = [{"price": px * (1 - d / 100), "usd": got / len(bb), "filled": False} for d in bb]
        self.sales.append({"q": q, "rebought": 0.0, "swept": 0.0, "day": i, "price": px, "levels": levels})
        self.log(i, why, px, -got, -q)

    # ---------------------------------------------------------------- ladder
    def arm(self, i):
        base = self.s.high(i, self.v["lookback"])
        self.rungs = {"base": base, "done": [False] * len(DIP), "frozen": False}
        close = self.s.d[i]["c"]
        self.passed = [k for k, dr in enumerate(DIP) if base * (1 - dr / 100) > close]

    def rung_price(self, k):
        return self.rungs["base"] * (1 - DIP[k] / 100)

    def rung_share(self):
        return 1.0 / max(1, self.rungs["done"].count(False))

    # ---------------------------------------------------------------- one day
    def snapshot(self):
        orders = []
        tp = self.v["tp"]
        for lot in self.lots:
            per = lot["cost"] / lot["q"] if lot["q"] > 0 else 0
            if lot["runner"]:
                orders.append(("runner", lot, per * 2.0))
                continue
            for k in (0, 1):
                if not lot["done"][k]:
                    orders.append(("tp", (lot, k), per * (1 + tp[k] / 100)))
        for s in self.sales:
            for lv in s["levels"]:
                if not lv["filled"]:
                    orders.append(("bb", (s, lv), lv["price"]))
        if self.entry == "ladder" and self.rungs:
            for k in range(len(DIP)):
                if not self.rungs["done"][k] and k not in self.passed:
                    orders.append(("rung", k, self.rung_price(k)))
        return orders

    def day(self, i):
        s, d = self.s, self.s.d
        c = d[i]
        if i == self.i0 or s.ym[i] != s.ym[i - 1]:
            amt = START_USD if i == self.i0 else MONTHLY_USD
            self.cash.append([i, amt])
            self.contributed += amt
        if i == self.i0:
            if self.entry == "ladder":
                self.arm(i)
            self.end_of_day(i)
            return
        orders = self.snapshot()
        # 1) market orders decided at yesterday's close, at today's open
        pend, self.pending = self.pending, []
        for a in pend:
            if a[0] == "buy":
                _, usd, why, rung = a
                if self.buy_in(i, usd, c["o"], TAKER, why) and rung is not None:
                    self.rungs["done"][rung] = True
                    self.rungs["frozen"] = True
            elif a[0] == "runner_exit":
                for lot in self.lots:
                    if lot["runner"] and lot["q"] > 0:
                        self.sell(i, lot, lot["q"], c["o"], TAKER, "runner exit (below 50-day average)")
                        lot["runner"] = False
                        lot["done"] = [True, True]
        # 2) resting sells
        bull_y = s.bull(i - 1)
        for kind, ref, px in orders:
            if kind == "tp":
                lot, k = ref
                if lot["done"][k] or lot["q"] <= 1e-15 or c["h"] < px:
                    continue
                fill = max(px, c["o"])
                lot["done"][k] = True
                if k == 0:
                    self.sell(i, lot, lot["sell_q"], fill, MAKER, f"sell +{self.v['tp'][0]}%")
                elif self.v["bull"] and bull_y:
                    self.sell(i, lot, lot["q"] / 2, fill, MAKER, f"sell +{self.v['tp'][1]}% (bull: half kept as runner)")
                    lot["runner"] = lot["q"] > 1e-15
                else:
                    self.sell(i, lot, lot["q"], fill, MAKER, f"sell +{self.v['tp'][1]}%")
            elif kind == "runner":
                lot = ref
                if lot["runner"] and lot["q"] > 0 and c["h"] >= px:
                    self.sell(i, lot, lot["q"], max(px, c["o"]), MAKER, "runner +100%")
                    lot["runner"] = False
        # 3) resting buys
        for kind, ref, px in orders:
            if kind == "bb":
                sale, lv = ref
                if lv["filled"] or c["l"] > px:
                    continue
                fill = min(px, c["o"])
                lv["filled"] = True
                q = lv["usd"] * (1 - MAKER) / fill
                self.fees += lv["usd"] * MAKER
                self.acb_buy(lv["usd"], q)
                self.buys += 1
                sale["rebought"] += q
                move = max(0.0, min(q, sale["rebought"] - sale["q"] - sale["swept"]))
                sale["swept"] += move
                self.core += move
                self.add_trade(i, q - move, lv["usd"] * (q - move) / q)
                self.log(i, "buy-back", fill, lv["usd"], q)
            elif kind == "rung":
                k = ref
                if self.rungs["done"][k] or c["l"] > px or not self.cap_ok(i):
                    continue
                if self.buy_in(i, self.waiting() * self.rung_share(), min(px, c["o"]), MAKER, f"buy-in rung {k + 1}"):
                    self.rungs["done"][k] = True
                    self.rungs["frozen"] = True
        keep = []
        for lot in self.lots:
            if lot["q"] <= 1e-12:
                continue
            if all(lot["done"]) and not lot["runner"]:
                self.core += lot["q"]           # a leftover too small to sell just stays held
                continue
            keep.append(lot)
        self.lots = keep
        self.sales = [x for x in self.sales if not all(lv["filled"] for lv in x["levels"])]
        self.end_of_day(i)

    def end_of_day(self, i):
        s, c = self.s, self.s.d[i]
        # stale buy-back money goes back to the buy-in cash
        if self.v["stale"]:
            for sale in self.sales:
                if i - sale["day"] >= STALE_DAYS and c["c"] > sale["price"]:
                    for lv in sale["levels"]:
                        if not lv["filled"]:
                            lv["filled"] = True
                            self.cash.append([i, lv["usd"]])
            self.sales = [x for x in self.sales if not all(lv["filled"] for lv in x["levels"])]
        # runner exit at the next open if the close fell below the 50-day average
        if any(lot["runner"] for lot in self.lots) and c["c"] < s.ma50[i]:
            self.pending.append(("runner_exit",))
        if i >= self.i1:
            return
        used = 0.0
        if self.entry == "ladder":
            r = self.rungs
            if all(r["done"]) or (r["frozen"] and c["c"] > r["base"]):
                self.arm(i)
            elif not r["frozen"] and not self.passed:
                self.arm(i)                 # nothing bought yet: trail the recent high
            if self.passed and i - self.last_market_rung >= PASSED_RUNG_GAP_DAYS and self.cap_ok(i):
                k = self.passed.pop(0)
                usd = self.waiting() * self.rung_share()
                self.pending.append(("buy", usd, f"buy-in rung {k + 1} (already passed: market)", k))
                self.last_market_rung = i
                used += usd
        elif self.entry == "random":
            if self.rng.random() < self.p_random and self.cap_ok(i):
                usd = self.waiting() * 0.5
                self.pending.append(("buy", usd, "random buy-in", None))
                used += usd
        old = sum(u for d0, u in self.cash if i + 1 - d0 >= MAX_WAIT_DAYS) - used
        if old >= MIN_ORDER and self.cap_ok(i):
            self.pending.append(("buy", old, f"waited {MAX_WAIT_DAYS} days: bought anyway", None))

    def run(self):
        for i in range(self.i0, self.i1 + 1):
            self.day(i)
            c = self.s.d[i]["c"]
            coins = self.core + sum(l["q"] for l in self.lots)
            w = self.waiting() + self.reserved()
            v = coins * c + w
            self.cash_share_sum += (w / v) if v > 0 else 1.0
        return self.result()

    def result(self):
        px = self.s.d[self.i1]["c"]
        trade = sum(l["q"] for l in self.lots)
        coins = self.core + trade
        waiting = self.waiting() + self.reserved()
        tax, carry = 0.0, 0.0
        for y in sorted(self.gains):
            net = self.gains[y] + carry
            if net > 0:
                tax += net * 0.5 * MARGINAL_TAX
                carry = 0.0
            else:
                carry = net
        days = self.i1 - self.i0 + 1
        score = coins + waiting / px
        return {"score": score, "coins": coins, "core": self.core, "trade": trade, "waiting": waiting,
                "contributed": self.contributed, "fees": self.fees, "tax": tax, "after_tax": score - tax / px,
                "buys": self.buys, "sells": self.sells, "buyins": self.buyins,
                "trades_per_month": (self.buys + self.sells) / (days / 30.4375),
                "cash_share": self.cash_share_sum / days, "events": self.events}


# ---------------------------------------------------------------- benchmarks
def dca(s, i0, i1):
    d, coins, contributed = s.d, 0.0, 0.0
    for i in range(i0, i1 + 1):
        if i == i0 or s.ym[i] != s.ym[i - 1]:
            amt = START_USD if i == i0 else MONTHLY_USD
            px = d[i]["c"] if i == i0 else d[i]["o"]
            coins += amt * (1 - TAKER) / px
            contributed += amt
    return {"score": coins, "coins": coins, "contributed": contributed}


def cash_matched(dca_res, share, end_px):
    """DCA that keeps the same share of its money in cash as the strategy did on average."""
    return dca_res["coins"] * (1 - share) + dca_res["contributed"] * share / end_px


# ---------------------------------------------------------------- experiments
def windows(n):
    out = []
    a = WARMUP
    while a + WINDOW_DAYS <= n - 1:
        out.append((a, a + WINDOW_DAYS))
        a += WINDOW_STEP
    return out


def regime(s, a, z):
    ch = s.d[z]["c"] / s.d[a]["c"] - 1
    return "bull" if ch > 1.0 else "bear" if ch < -0.5 else "sideways"


def variants():
    out = []
    for lots in ("buy", "month", "avg"):
        for core in (0.4, 0.5, 0.7):
            for bull in (False, True):
                for stale in (False, True):
                    out.append(dict(BASE, lots=lots, core=core, bull=bull, stale=stale))
    return out


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    return (xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2) if n else None


def pct(xs, p):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, max(0, int(round(p * (len(xs) - 1)))))] if xs else None


def summarize(rows):
    """rows: dicts with vs_dca, vs_cm, after_tax_vs_dca, regime, trades_pm, cash_share (fractions)."""
    if not rows:
        return {}
    vs = [r["vs_dca"] for r in rows]
    out = {"windows": len(rows),
           "beat_dca_pct": round(100 * sum(x > 0 for x in vs) / len(vs), 1),
           "beat_cash_matched_pct": round(100 * sum(r["vs_cm"] > 0 for r in rows) / len(rows), 1),
           "median_vs_dca_pct": round(100 * median(vs), 2),
           "p10_vs_dca_pct": round(100 * pct(vs, 0.10), 2),
           "worst_vs_dca_pct": round(100 * min(vs), 2),
           "median_vs_cash_matched_pct": round(100 * median([r["vs_cm"] for r in rows]), 2),
           "after_tax_median_vs_dca_pct": round(100 * median([r["after_tax_vs_dca"] for r in rows]), 2),
           "trades_per_month": round(median([r["trades_pm"] for r in rows]), 1),
           "cash_share_pct": round(100 * median([r["cash_share"] for r in rows]), 1)}
    for g in ("bull", "bear", "sideways"):
        sub = [r["vs_dca"] for r in rows if r["regime"] == g]
        out[f"{g}_windows"] = len(sub)
        out[f"{g}_median_vs_dca_pct"] = round(100 * median(sub), 2) if sub else None
    return out


def run_window(s, a, z, v, **kw):
    r = Sim(s, a, z, v, **kw).run()
    base = dca(s, a, z)
    px = s.d[z]["c"]
    return {"vs_dca": r["score"] / base["score"] - 1,
            "vs_cm": r["score"] / cash_matched(base, r["cash_share"], px) - 1,
            "after_tax_vs_dca": r["after_tax"] / base["score"] - 1,
            "regime": regime(s, a, z), "trades_pm": r["trades_per_month"], "cash_share": r["cash_share"],
            "buyins": r["buyins"], "raw": r}


def bootstrap_series(s, rng, n_days, block=30):
    """A synthetic price path stitched from random 30-day chunks of the real history (as ratios)."""
    d = s.d
    out, prev = [], 100.0
    t = 1_600_000_000 - (1_600_000_000 % DAY)
    while len(out) < n_days:
        a = rng.randrange(1, len(d) - block)
        for j in range(a, a + block):
            pc = d[j - 1]["c"]
            c = {"t": t, "o": prev * d[j]["o"] / pc, "h": prev * d[j]["h"] / pc, "l": prev * d[j]["l"] / pc,
                 "c": prev * d[j]["c"] / pc, "v": 0.0}
            out.append(c)
            prev = c["c"]
            t += DAY
            if len(out) >= n_days:
                break
    return Series(out)


_SERIES = {}


def _init(series):
    _SERIES.update(series)


def _job_windows(args):
    sym, v = args
    s = _SERIES[sym]
    out = []
    for a, z in windows(len(s.d)):
        r = run_window(s, a, z, v)
        r.pop("raw")
        out.append(r)
    return sym, vname(v), out


def _job_deep(args):
    sym, v, seeds, paths, seed = args
    s = _SERIES[sym]
    beat = []
    for a, z in windows(len(s.d)):
        r = Sim(s, a, z, v).run()
        p = r["buyins"] / max(1, z - a)
        rs = [Sim(s, a, z, v, entry="random", p_random=p, seed=sd).run()["score"] for sd in range(seeds)]
        beat.append(r["score"] > median(rs))
    rng = random.Random(seed)
    boot = []
    for _ in range(paths):
        b = bootstrap_series(s, rng, WARMUP + WINDOW_DAYS + 1)
        r = Sim(b, WARMUP, WARMUP + WINDOW_DAYS, v).run()
        boot.append(r["score"] / dca(b, WARMUP, WARMUP + WINDOW_DAYS)["score"] - 1)
    return sym, vname(v), beat, boot


def pool_map(fn, jobs, series, procs=None):
    procs = procs or min(8, os.cpu_count() or 1)
    if procs <= 1:
        _init(series)
        return [fn(j) for j in jobs]
    import multiprocessing as mp
    with mp.get_context("fork").Pool(procs, initializer=_init, initargs=(series,)) as p:
        return p.map(fn, jobs, chunksize=1)


def overall(coin_rows):
    """Pool every window of every coin per variant."""
    pooled = {}
    for per_variant in coin_rows:
        for k, (v, rows) in per_variant.items():
            pooled.setdefault(k, []).extend(rows)
    table = []
    for k, rows in pooled.items():
        sm = summarize(rows)
        table.append(dict(sm, variant=k))
    table.sort(key=lambda r: (-r["beat_cash_matched_pct"], -r["after_tax_median_vs_dca_pct"]))
    return table


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="site/stress.json")
    ap.add_argument("--coins", default="ETH,SOL,XRP,QNT,BTC")
    ap.add_argument("--reuse", default=os.environ.get("PREV_STRESS_URL", ""))
    ap.add_argument("--offline", action="store_true", help="synthetic history (tests)")
    a = ap.parse_args()
    import watch  # noqa: E402  (for VERSION and get_json)
    if a.reuse:
        try:
            prev = watch.get_json(a.reuse + "?t=" + str(int(time.time())))
            age = time.time() - datetime.fromisoformat(prev["generated_at"]).timestamp()
            if age < 20 * 3600 and prev.get("version") == watch.VERSION:
                os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
                json.dump(prev, open(a.out, "w"))
                print("reused stress test from", prev["generated_at"])
                return
        except Exception as e:  # noqa: BLE001
            print("no reusable stress test:", e)
    t0 = time.time()
    vs = variants()
    coins, series = [], {}
    for sym in a.coins.split(","):
        try:
            if a.offline:
                daily, meta = synthetic(sym), {"source": "synthetic test data"}
            else:
                import history
                daily, meta = history.load(sym)
            n = len(windows(len(daily)))
            if not n:
                coins.append({"symbol": sym, "error": "not enough history for a 2-year window", "data": meta})
                continue
            series[sym] = Series(daily)
            coins.append({"symbol": sym, "windows": n, "data": meta})
            print(f"{sym}: {len(daily)} days, {n} windows ({time.time() - t0:.0f}s)")
        except Exception as e:  # noqa: BLE001
            print(f"{sym} failed: {e}", file=sys.stderr)
            coins.append({"symbol": sym, "error": str(e)[:200]})
    lv = {vname(v): v for v in vs}
    rows = {}                                   # (sym, variant) -> rows
    for sym, k, r in pool_map(_job_windows, [(sym, v) for sym in series for v in vs], series):
        rows[(sym, k)] = r
    print(f"sweep done ({time.time() - t0:.0f}s)")
    for c in coins:
        if c["symbol"] in series:
            c["variants"] = {k: summarize(rows[(c["symbol"], k)]) for k in lv}
    table = []
    for k in lv:
        pooled = [r for sym in series for r in rows[(sym, k)]]
        table.append(dict(summarize(pooled), variant=k))
    table.sort(key=lambda r: (-r["beat_cash_matched_pct"], -r["after_tax_median_vs_dca_pct"]))
    leaders = [r["variant"] for r in table[:3]]
    if vname(BASE) not in leaders:
        leaders.append(vname(BASE))
    deep_raw = pool_map(_job_deep, [(sym, lv[k], 3, 30, 11) for sym in series for k in leaders], series)
    deep = {}
    for k in leaders:
        beat = [b for sym, kk, bt, bo in deep_raw if kk == k for b in bt]
        boot = [b for sym, kk, bt, bo in deep_raw if kk == k for b in bo]
        deep[k] = {"beat_random_timing_pct": round(100 * sum(beat) / max(1, len(beat)), 1),
                   "bootstrap_paths": len(boot),
                   "bootstrap_beat_dca_pct": round(100 * sum(x > 0 for x in boot) / max(1, len(boot)), 1),
                   "bootstrap_p10_vs_dca_pct": round(100 * pct(boot, 0.1), 2) if boot else None,
                   "bootstrap_median_vs_dca_pct": round(100 * median(boot), 2) if boot else None,
                   "bootstrap_p90_vs_dca_pct": round(100 * pct(boot, 0.9), 2) if boot else None}
    print(f"deep checks done ({time.time() - t0:.0f}s)")
    best = lv[table[0]["variant"]] if table else BASE
    tests = (("baseline 30 days", {}), ("baseline 60 days", {"lookback": 60}), ("baseline 90 days", {"lookback": 90}),
             ("sells +15/+30, buy-backs -15/-30", {"tp": (15, 30), "bb": (15, 30)}),
             ("sells +30/+60, buy-backs -30/-60", {"tp": (30, 60), "bb": (30, 60)}))
    sres = pool_map(_job_windows, [(sym, dict(best, **ch)) for _, ch in tests for sym in series], series)
    sens = []
    for j, (name, ch) in enumerate(tests):
        part = [r for sym, k, rs in sres[j * len(series):(j + 1) * len(series)] for r in rs]
        sens.append(dict(summarize(part), test=name))
    # one fully logged run of the leader on each coin's latest window, so trades can be checked by hand
    for c in coins:
        if c["symbol"] in series:
            s_ = series[c["symbol"]]
            a_, z_ = windows(len(s_.d))[-1]
            r = Sim(s_, a_, z_, best, record=True).run()
            c["example"] = {"variant": vname(best), "start": ymd(s_.d[a_]["t"]), "end": ymd(s_.d[z_]["t"]),
                            "score": round(r["score"], 8), "dca": round(dca(s_, a_, z_)["score"], 8),
                            "core": round(r["core"], 8), "waiting": round(r["waiting"], 2), "tax": round(r["tax"], 2),
                            "events": r["events"]}
    # your plan, replayed: $30 per coin on the 1st and 15th, as far back as 10 years
    import myplan
    mp = {"amount": 30.0, "days": [1, 15], "core_pct": 70, "coins": {}}
    for sym in ("ETH", "SOL"):
        if sym in series:
            d_ = series[sym].d
            st_ = myplan.start_index(d_, 10)
            r = myplan.replay(d_, amount=30.0, start=st_)
            rnd = lambda dd: {k: (round(v, 8) if isinstance(v, float) else v) for k, v in dd.items()}
            mp["coins"][sym] = rnd(r)
            mp["coins"][sym]["pivot_wait"] = rnd(myplan.pivot_hold(d_, amount=30.0, start=st_, fallback=False))
            mp["coins"][sym]["pivot_fallback"] = rnd(myplan.pivot_hold(d_, amount=30.0, start=st_, fallback=True))
            mp["coins"][sym]["models"] = {m: rnd(myplan.buy_model(d_, st_, m, amount=30.0)) for m in myplan.BUY_MODELS}
    if mp["coins"]:
        mp["total"] = {k: round(sum(c[k] for c in mp["coins"].values()), 2) for k in ("invested", "value", "dca_value", "cash")}
    doc_myplan = mp
    doc = {"generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "version": watch.VERSION,
           "my_plan": doc_myplan,
           "seconds": round(time.time() - t0, 1),
           "assumptions": {
               "money": f"${START_USD:,.0f} to start, ${MONTHLY_USD:,.0f} on the 1st of each month after, per coin",
               "limits": f"at most {MAX_BUYINS_MONTH} buy-ins a month; passed rungs at most one a week; ${MIN_ORDER:.0f} minimum order",
               "fees": f"{MAKER * 100:.2f}% on resting limit orders, {TAKER * 100:.2f}% on market orders (Kraken Pro)",
               "fills": "limit buys at min(order, open), limit sells at max(order, open); new orders wait a day; daily candles",
               "windows": f"rolling {WINDOW_DAYS // 365}-year windows every {WINDOW_STEP} days; bull = coin more than doubled, bear = fell more than half",
               "cash_matched": "DCA that keeps the same average share of cash as the strategy; beating it means the rules add something beyond just holding cash",
               "tax": f"Canadian estimate: average cost (ACB) per coin, 50% of net gains taxed at an assumed {int(MARGINAL_TAX * 100)}% marginal rate, losses carried forward; superficial-loss denials and business-income treatment not modelled",
               "random": "same rules, but buy-ins at random days (same number) instead of the dip ladder",
               "bootstrap": "synthetic 2-year paths stitched from random 30-day chunks of each coin's real history"},
           "coins": coins, "overall": table, "deep": deep, "sensitivity": {"variant": vname(best), "tests": sens}}
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w") as f:
        json.dump(doc, f, indent=1)
    print(f"done in {time.time() - t0:.0f}s")
    for r in table[:8]:
        print(f"{r['variant']:45s} beatDCA {r['beat_dca_pct']:5.1f}%  beatCM {r['beat_cash_matched_pct']:5.1f}%  "
              f"med {r['median_vs_dca_pct']:+6.1f}%  p10 {r['p10_vs_dca_pct']:+6.1f}%  bull {r['bull_median_vs_dca_pct']}  tax {r['after_tax_median_vs_dca_pct']:+6.1f}%")


def synthetic(sym, n=2600):
    """Deterministic fake history with a bubble, a crash and a sideways stretch (offline tests only)."""
    r = random.Random(sym)
    px, out, t = 10.0, [], 1_450_000_000 - 1_450_000_000 % DAY
    for i in range(n):
        drift = 0.004 if i % 1400 < 400 else (-0.003 if i % 1400 < 750 else 0.0002)
        o = px
        px = max(0.01, px * math.exp(drift + r.gauss(0, 0.04)))
        out.append({"t": t + i * DAY, "o": o, "h": max(o, px) * (1 + abs(r.gauss(0, 0.01))),
                    "l": min(o, px) * (1 - abs(r.gauss(0, 0.01))), "c": px, "v": 1.0})
    return out


if __name__ == "__main__":
    main()
