"""Replay YOUR plan (app v5.1) on real daily history.

  - buy $AMOUNT of each coin on the 1st and 15th, whatever the price (market order, taker fee)
  - 70% to Core (never sold), 30% to that month's Trade lot (merged until the lot first sells)
  - each lot: sell half at +20%, the rest at +40% (limit orders, maker fee)
  - bull mode: above a rising 200-day average, the +40% sell keeps half as a runner,
    sold at +100% or at the next open after a close below the 50-day average
  - each sale: buy-backs -20% / -40%, half the money each; extra coins bought back go to Core
  - monthly re-baseline: unfilled buy-backs from an earlier month are cancelled on the 1st;
    their cash joins the next 3 buy days, all to Trade
  - $5 minimum order (Kraken); orders created during a day can't fill until the next day;
    limit fills at the gap price (min/max of order and open)
Compared with plain buying on the same days (no selling).
"""
from datetime import datetime, timezone

MAKER, TAKER, MIN_ORDER = 0.0025, 0.0040, 5.0
TP, BB = (20, 40), (20, 40)
ROLL_DAYS = 3


def _d(t):
    return datetime.fromtimestamp(t, timezone.utc)


def averages(d, n):
    out, s = [], 0.0
    for i, c in enumerate(d):
        s += c["c"]
        if i >= n:
            s -= d[i - n]["c"]
        out.append(s / min(i + 1, n) if i + 1 >= n else None)
    return out


def replay(daily, amount=10.0, days=(1, 15), core=0.7, bull=True, start=0):
    ma200, ma50 = averages(daily, 200), averages(daily, 50)

    def is_bull(i):
        return i >= 220 and ma200[i] and ma200[i - 20] and daily[i]["c"] > ma200[i] > ma200[i - 20]

    st = {"core": 0.0, "lots": [], "sales": [], "roll": 0.0, "left": 0, "fees": 0.0, "invested": 0.0,
          "buys": 0, "sells": 0, "buybacks": 0, "rolled": 0.0, "to_core_from_trades": 0.0, "nid": 1}
    dca = {"coins": 0.0}
    pending_exit = False

    def add_trade(q, usd, month):
        if q <= 1e-15:
            return
        l = next((x for x in st["lots"] if x["month"] == month and not x["sold"] and not x["runner"]), None)
        if l:
            l["q"] += q
            l["cost"] += usd
            l["q0"] = l["q"]
            l["done"] = [False, False]
        else:
            st["lots"].append({"id": st["nid"], "month": month, "q": q, "q0": q, "cost": usd,
                               "done": [False, False], "runner": False, "sold": False})
            st["nid"] += 1

    def sell(l, q, px, fee, i):
        if q * px < MIN_ORDER:
            if l["q"] * px < MIN_ORDER:
                return False
            q = l["q"]
        q = min(q, l["q"])
        per = l["cost"] / l["q"]
        got = q * px * (1 - fee)
        st["fees"] += q * px * fee
        l["q"] -= q
        l["cost"] -= per * q
        l["sold"] = True
        st["sells"] += 1
        levels = ([{"price": px * (1 - BB[0] / 100), "usd": got, "done": False}] if got / 2 < MIN_ORDER else
                  [{"price": px * (1 - p / 100), "usd": got / 2, "done": False} for p in BB])
        st["sales"].append({"q": q, "rebought": 0.0, "swept": 0.0, "month": _d(daily[i]["t"]).strftime("%Y-%m"),
                            "levels": levels})
        return True

    for i in range(start, len(daily)):
        c = daily[i]
        dt = _d(c["t"])
        month = dt.strftime("%Y-%m")
        # monthly re-baseline on the 1st (or the first day of a new month)
        if i > start and month != _d(daily[i - 1]["t"]).strftime("%Y-%m"):
            for s in st["sales"]:
                if s["month"] < month:
                    for lv in s["levels"]:
                        if not lv["done"]:
                            lv["done"] = True
                            st["roll"] += lv["usd"]
                            st["rolled"] += lv["usd"]
                            st["left"] = ROLL_DAYS
            st["sales"] = [s for s in st["sales"] if any(not lv["done"] for lv in s["levels"])]
        # snapshot of resting orders at the start of the day
        snap = []
        for l in st["lots"]:
            per = l["cost"] / l["q"] if l["q"] > 0 else 0
            if l["runner"]:
                snap.append(("runner", l, None, per * 2))
            else:
                for k in (0, 1):
                    if not l["done"][k]:
                        snap.append(("tp", l, k, per * (1 + TP[k] / 100)))
        for s in st["sales"]:
            for lv in s["levels"]:
                if not lv["done"]:
                    snap.append(("bb", s, lv, lv["price"]))
        # runner exit decided at yesterday's close
        if pending_exit:
            for l in st["lots"]:
                if l["runner"] and l["q"] > 0:
                    sell(l, l["q"], c["o"], TAKER, i)
                    l["runner"] = False
                    l["done"] = [True, True]
            pending_exit = False
        # buy day (market order at the open)
        if dt.day in days:
            extra = st["roll"] / st["left"] if st["left"] > 0 and st["roll"] > 0.005 else 0.0
            usd = amount + extra
            q = usd * (1 - TAKER) / c["o"]
            st["fees"] += usd * TAKER
            st["invested"] += amount
            st["roll"] -= extra
            if extra:
                st["left"] -= 1
            core_usd = amount * core
            st["core"] += core_usd * (1 - TAKER) / c["o"]
            add_trade(q - core_usd * (1 - TAKER) / c["o"], usd - core_usd, month)
            st["buys"] += 1
            dca["coins"] += amount * (1 - TAKER) / c["o"]
        # resting sells, then resting buys
        bull_y = bull and is_bull(i - 1)
        for kind, l, k, px in snap:
            if kind == "tp" and not l["done"][k] and l["q"] > 1e-15 and c["h"] >= px:
                fill = max(px, c["o"])
                l["done"][k] = True
                if k == 0:
                    sell(l, l["q0"] / 2, fill, MAKER, i)
                elif bull_y:
                    sell(l, l["q"] / 2, fill, MAKER, i)
                    l["runner"] = l["q"] > 1e-15
                else:
                    sell(l, l["q"], fill, MAKER, i)
            elif kind == "runner" and l["runner"] and l["q"] > 0 and c["h"] >= px:
                sell(l, l["q"], max(px, c["o"]), MAKER, i)
                l["runner"] = False
                l["done"] = [True, True]
        for kind, s, lv, px in snap:
            if kind == "bb" and not lv["done"] and c["l"] <= px:
                fill = min(px, c["o"])
                lv["done"] = True
                q = lv["usd"] * (1 - MAKER) / fill
                st["fees"] += lv["usd"] * MAKER
                s["rebought"] += q
                st["buybacks"] += 1
                extra = max(0.0, min(q, s["rebought"] - s["q"] - s["swept"]))
                s["swept"] += extra
                st["core"] += extra
                st["to_core_from_trades"] += extra
                add_trade(q - extra, lv["usd"] * (q - extra) / q, month)
        # tidy: finished lots; leftovers too small to sell stay held in Core
        keep = []
        for l in st["lots"]:
            if l["q"] <= 1e-15:
                continue
            if all(l["done"]) and not l["runner"]:
                st["core"] += l["q"]
                continue
            keep.append(l)
        st["lots"] = keep
        st["sales"] = [s for s in st["sales"] if any(not lv["done"] for lv in s["levels"])]
        if any(l["runner"] for l in st["lots"]) and ma50[i] and c["c"] < ma50[i]:
            pending_exit = True

    px = daily[-1]["c"]
    trade = sum(l["q"] for l in st["lots"])
    cash = st["roll"] + sum(lv["usd"] for s in st["sales"] for lv in s["levels"] if not lv["done"])
    coins = st["core"] + trade
    return {"from": _d(daily[start]["t"]).strftime("%Y-%m-%d"), "to": _d(daily[-1]["t"]).strftime("%Y-%m-%d"),
            "price_now": px, "invested": round(st["invested"], 2), "buy_days": st["buys"],
            "coins": coins, "core": st["core"], "trade": trade, "cash": round(cash, 2),
            "value": round(coins * px + cash, 2), "fees": round(st["fees"], 2),
            "sells": st["sells"], "buybacks": st["buybacks"], "rolled_over_cash": round(st["rolled"], 2),
            "coins_to_core_from_trades": st["to_core_from_trades"],
            "dca_coins": dca["coins"], "dca_value": round(dca["coins"] * px, 2)}


def start_index(daily, years):
    t0 = daily[-1]["t"] - int(years * 365.25 * 86400)
    return next((i for i, c in enumerate(daily) if c["t"] >= t0), 0)


def monthly_pivots(daily):
    """Traditional pivots for each month from the PREVIOUS calendar month's high, low and close
    (what TradingView's 'Pivots Traditional' shows on a monthly anchor). month -> (P, S1, S2)."""
    by_m = {}
    for c in daily:
        m = _d(c["t"]).strftime("%Y-%m")
        b = by_m.setdefault(m, {"h": c["h"], "l": c["l"], "c": c["c"]})
        b["h"], b["l"], b["c"] = max(b["h"], c["h"]), min(b["l"], c["l"]), c["c"]
    ms = sorted(by_m)
    out = {}
    for prev, cur in zip(ms, ms[1:]):
        h, l, c = by_m[prev]["h"], by_m[prev]["l"], by_m[prev]["c"]
        p = (h + l + c) / 3
        out[cur] = (p, 2 * p - h, p - (h - l))
    return out


def pivot_hold(daily, amount=30.0, days=(1, 15), start=0, fallback=False):
    """Buy-and-hold, but each buy day's money waits as two limit orders: half at S1, half at S2.
    fallback=False: unfilled money moves to the new month's S1/S2 and keeps waiting.
    fallback=True: money still unfilled when a new month starts is bought at market at that open."""
    piv = monthly_pivots(daily)
    coins = cash_s1 = cash_s2 = invested = fees = 0.0
    fills = market = 0
    for i in range(start, len(daily)):
        c = daily[i]
        dt = _d(c["t"])
        m = dt.strftime("%Y-%m")
        new_month = i > start and m != _d(daily[i - 1]["t"]).strftime("%Y-%m")
        if new_month and fallback and cash_s1 + cash_s2 >= MIN_ORDER:
            usd = cash_s1 + cash_s2
            coins += usd * (1 - TAKER) / c["o"]
            fees += usd * TAKER
            cash_s1 = cash_s2 = 0.0
            market += 1
        lv = piv.get(m)
        if lv and i > start:                       # orders placed before today can fill today
            for side in ("s1", "s2"):
                px = lv[1] if side == "s1" else lv[2]
                pool = cash_s1 if side == "s1" else cash_s2
                if pool >= MIN_ORDER and px > 0 and c["l"] <= px:
                    fill = min(px, c["o"])
                    coins += pool * (1 - MAKER) / fill
                    fees += pool * MAKER
                    fills += 1
                    if side == "s1":
                        cash_s1 = 0.0
                    else:
                        cash_s2 = 0.0
        if dt.day in days:                         # new money goes onto the orders (fills from tomorrow)
            invested += amount
            cash_s1 += amount / 2
            cash_s2 += amount / 2
    px = daily[-1]["c"]
    cash = cash_s1 + cash_s2
    return {"invested": round(invested, 2), "coins": coins, "cash": round(cash, 2),
            "value": round(coins * px + cash, 2), "fees": round(fees, 2), "limit_fills": fills, "market_buys": market}
