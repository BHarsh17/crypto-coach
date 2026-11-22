"""Accumulation backtest: start with nothing, add money every month, grow a Core bucket.

Every model gets the same money: MONTHLY_USD on the first day of each month.
What changes is WHEN that money is used to buy in.

  A        dip ladder: limit buys at -10/-25/-40/-55% below the 30-day high.
           Each rung uses an equal share of the cash waiting.
  B_strict washed out (3+ bottom signals in the last 14 days) AND price closes back above
           its 20-day average  ->  buy with all the cash waiting, at the next day's open.
  B_loose  same, but 2+ bottom signals.
  Money never waits forever: monthly money no signal used within MAX_WAIT_DAYS is bought
  at the next open anyway, so a strong uptrend can't leave you sitting in cash.
  A_lots   same buy-ins as A, but every buy is its own lot with its own take-profits
           (+20/+40% of THAT buy's price) instead of one average for the whole bucket.
  dca      buy every month the day the money arrives. Never sells. The benchmark.

Once bought (A and B), every buy is split CORE_SHARE into Core (never sold) and the
rest into the Trade bucket, which runs the v4 ladder:
  - base = average cost of the trade coins
  - take-profits at base +20% and +40%, each selling half the trade coins
  - each sale gets buy-backs 20% and 40% below the sale price, half the sale money each
  - once a sale's buy-backs have bought back MORE coins than were sold, the extra coins
    move to Core (the gains are locked in as coins you keep)

Honesty rules (same as the main backtest):
  - an order only fills if it existed at the start of the day (no same-day look-ahead)
  - signals are read at the day's close and acted on at the NEXT day's open
  - fees on every trade
Score: coin_equiv = coins + cash still waiting converted at the final price.
"""
from datetime import datetime, timezone

DAY = 86400
FEE = 0.0025
MONTHLY_USD = 100.0
CORE_SHARE = 0.4
TP = [20, 40]            # take-profits, % above base
TP_SHARE = 0.5           # each take-profit sells this share of the trade coins at the last reset
BB = [20, 40]            # buy-backs, % below each sale
DIP = [10, 25, 40, 55]   # model A rungs, % below the 30-day high
LOOKBACK_HIGH = 30
WASHED_WINDOW = 14
MIN_ORDER_USD = 5.0      # smaller orders aren't placed (Kraken has minimum order sizes)
MAX_WAIT_DAYS = 60       # monthly money that no signal used within this many days is bought anyway
MODELS = ("A", "A_lots", "B_strict", "B_loose", "dca")
START_STEP_DAYS = 91     # stress test: also start 3, 6 and 9 months later
STARTS = 4


def day(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def month(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m")


def sma20(daily, i):
    w = daily[max(0, i - 19): i + 1]
    return sum(c["c"] for c in w) / len(w)


class Book:
    def __init__(self):
        self.core = 0.0
        self.trade = 0.0
        self.trade_cost = 0.0      # USD cost of the trade coins (for the average)
        self.cash = 0.0            # monthly money not yet used
        self.cash_lots = []        # [day_index, usd] oldest first, sums to self.cash
        self.fees = 0.0
        self.contributed = 0.0
        self.sales = []            # open sales waiting for buy-backs
        self.tp = None             # {"base", "q", "done": [..]}
        self.events = []
        self.buys = self.sells = 0
        self.added_trade = False   # trade coins added today -> reset take-profits at the close

    @staticmethod
    def buyback_levels(px, got):
        """Two buy-backs, half the money each; one at -20% with all of it if halves would be under the minimum."""
        if got / len(BB) < MIN_ORDER_USD:
            return [{"price": px * (1 - BB[0] / 100), "usd": got, "filled": False, "new": True}]
        return [{"price": px * (1 - d / 100), "usd": got / len(BB), "filled": False, "new": True} for d in BB]

    def reserved(self):
        return sum(lv["usd"] for s in self.sales for lv in s["levels"] if not lv["filled"])

    def buy_in(self, usd, px, t, why, split=True):
        usd = min(usd, self.cash)
        if usd < MIN_ORDER_USD:
            return
        q = usd * (1 - FEE) / px
        self.cash -= usd
        left = usd
        while left > 1e-9 and self.cash_lots:          # spend the oldest money first
            take = min(left, self.cash_lots[0][1])
            self.cash_lots[0][1] -= take
            left -= take
            if self.cash_lots[0][1] <= 1e-9:
                self.cash_lots.pop(0)
        self.fees += usd * FEE
        core = q * CORE_SHARE if split else q
        self.core += core
        if q - core > 0:
            self.trade += q - core
            self.trade_cost += usd * (1 - CORE_SHARE) if split else 0
            self.added_trade = True
        self.buys += 1
        self.events.append([day(t), why, round(px, 6), round(usd, 2), round(q, 8)])

    def reset_tp(self):
        if self.trade > 1e-12:
            self.tp = {"base": self.trade_cost / self.trade, "q": self.trade * TP_SHARE, "done": [False] * len(TP)}
        else:
            self.tp = None
        self.added_trade = False

    def orders(self):
        """Snapshot of the orders that exist at the start of a day."""
        out = []
        if self.tp:
            for k, p in enumerate(TP):
                if not self.tp["done"][k]:
                    out.append(("tp", k, self.tp["base"] * (1 + p / 100)))
        for s in self.sales:
            for lv in s["levels"]:
                if not lv["filled"]:
                    out.append(("bb", lv, lv["price"]))
        return out

    def fill(self, orders, c):
        # sells first (snapshot prices), then buy-backs; anything they create waits for tomorrow
        for kind, ref, px in orders:
            if kind != "tp" or c["h"] < px or self.tp is None or self.tp["done"][ref]:
                continue
            self.tp["done"][ref] = True
            q = min(self.trade, self.tp["q"])
            if q * px < MIN_ORDER_USD:
                if self.trade * px < MIN_ORDER_USD:
                    continue
                q = self.trade
            avg = self.trade_cost / self.trade
            got = q * px * (1 - FEE)
            self.trade -= q
            self.trade_cost -= avg * q
            self.fees += q * px * FEE
            self.sells += 1
            self.sales.append({"q": q, "price": px, "rebought": 0.0, "swept": 0.0, "levels": self.buyback_levels(px, got)})
            self.events.append([day(c["t"]), f"JUMP OUT: take-profit +{TP[ref]}%", round(px, 6), round(-got, 2), round(-q, 8)])
        for kind, lv, px in orders:
            if kind != "bb" or lv["filled"] or lv.get("new") or c["l"] > px:
                continue
            lv["filled"] = True
            q = lv["usd"] * (1 - FEE) / px
            self.fees += lv["usd"] * FEE
            self.trade += q
            self.trade_cost += lv["usd"]
            self.added_trade = True
            self.buys += 1
            sale = next(s for s in self.sales if lv in s["levels"])
            sale["rebought"] += q
            self.events.append([day(c["t"]), "JUMP IN: buy-back", round(px, 6), round(lv["usd"], 2), round(q, 8)])
            extra = sale["rebought"] - sale["q"] - sale["swept"]
            if extra > 1e-12:
                move = min(extra, self.trade)
                avg = self.trade_cost / self.trade
                self.trade -= move
                self.trade_cost -= avg * move
                self.core += move
                sale["swept"] += move
                self.events.append([day(c["t"]), "Gain moved to Core", round(px, 6), 0.0, round(move, 8)])
        self.sales = [s for s in self.sales if not all(lv["filled"] for lv in s["levels"])]
        for s in self.sales:
            for lv in s["levels"]:
                lv.pop("new", None)

    def result(self, end_px):
        waiting = self.cash + self.reserved()
        coins = self.core + self.trade
        return {"coins": round(coins, 8), "core": round(self.core, 8), "trade": round(self.trade, 8),
                "cash": round(waiting, 2), "contributed": round(self.contributed, 2),
                "value": round(coins * end_px + waiting, 2),
                "coin_equiv": round(coins + waiting / end_px, 8),
                "fees": round(self.fees, 2), "buys": self.buys, "sells": self.sells, "trims": self.sells,
                "events": self.events}


class LotBook(Book):
    """Every buy (and every buy-back) is its own lot with its own take-profits."""

    def __init__(self):
        super().__init__()
        self.lots = []             # {"q", "cost", "sell_q", "done": [..], "new": bool}

    def _add_lot(self, q, usd):
        if q > 1e-12:
            self.lots.append({"q": q, "cost": usd / q, "sell_q": q * TP_SHARE, "done": [False] * len(TP), "new": True})

    def buy_in(self, usd, px, t, why, split=True):
        before = self.trade
        cost_before = self.trade_cost
        super().buy_in(usd, px, t, why, split)
        if self.trade > before:
            self._add_lot(self.trade - before, self.trade_cost - cost_before)
        self.added_trade = False

    def reset_tp(self):
        self.added_trade = False

    def orders(self):
        out = []
        for lot in self.lots:
            if lot.get("new"):
                continue
            for k, p in enumerate(TP):
                if not lot["done"][k]:
                    out.append(("tp", (lot, k), lot["cost"] * (1 + p / 100)))
        for s in self.sales:
            for lv in s["levels"]:
                if not lv["filled"]:
                    out.append(("bb", lv, lv["price"]))
        return out

    def fill(self, orders, c):
        for kind, ref, px in orders:
            if kind != "tp" or c["h"] < px:
                continue
            lot, k = ref
            if lot["done"][k]:
                continue
            lot["done"][k] = True
            q = min(lot["sell_q"], lot["q"])
            if q * px < MIN_ORDER_USD:
                if lot["q"] * px < MIN_ORDER_USD:
                    continue                    # too small to sell: it just stays held
                q = lot["q"]
                lot["done"] = [True] * len(TP)
            got = q * px * (1 - FEE)
            lot["q"] -= q
            self.trade -= q
            self.trade_cost -= lot["cost"] * q
            self.fees += q * px * FEE
            self.sells += 1
            self.sales.append({"q": q, "price": px, "rebought": 0.0, "swept": 0.0, "levels": self.buyback_levels(px, got)})
            self.events.append([day(c["t"]), f"JUMP OUT: lot take-profit +{TP[k]}%", round(px, 6), round(-got, 2), round(-q, 8)])
        for kind, lv, px in orders:
            if kind != "bb" or lv["filled"] or c["l"] > px:
                continue
            lv["filled"] = True
            q = lv["usd"] * (1 - FEE) / px
            self.fees += lv["usd"] * FEE
            self.buys += 1
            sale = next(s for s in self.sales if lv in s["levels"])
            sale["rebought"] += q
            self.events.append([day(c["t"]), "JUMP IN: buy-back", round(px, 6), round(lv["usd"], 2), round(q, 8)])
            move = max(0.0, min(q, sale["rebought"] - sale["q"] - sale["swept"]))
            keep = q - move
            self.core += move
            sale["swept"] += move
            self.trade += keep
            self.trade_cost += lv["usd"] * keep / q
            self._add_lot(keep, lv["usd"] * keep / q)
            if move > 1e-12:
                self.events.append([day(c["t"]), "Gain moved to Core", round(px, 6), 0.0, round(move, 8)])
        self.sales = [s for s in self.sales if not all(lv["filled"] for lv in s["levels"])]
        self.lots = [lot for lot in self.lots if not all(lot["done"]) and lot["q"] > 1e-12]

    def end_day(self):
        for lot in self.lots:
            lot.pop("new", None)
        for s in self.sales:
            for lv in s["levels"]:
                lv.pop("new", None)

    def result(self, end_px):
        r = super().result(end_px)
        r["open_lots"] = len(self.lots)
        return r


def run(daily, i0, i1, signals, model, monthly=MONTHLY_USD):
    """One model over daily[i0..i1]. signals(i) -> (top_flags, bottom_signals) at day i's close."""
    b = LotBook() if model == "A_lots" else Book()
    is_a = model in ("A", "A_lots")
    need = {"B_strict": 3, "B_loose": 2}.get(model)
    rungs = None            # model A: {"anchor", "done": [..]}
    pending_b = False

    def arm(i):
        hi = max(c["h"] for c in daily[max(0, i - LOOKBACK_HIGH + 1): i + 1])
        return {"anchor": hi, "done": [False] * len(DIP)}

    if is_a:
        rungs = arm(i0)
    washed = []             # days (index) with enough bottom signals
    for i in range(i0, i1 + 1):
        c = daily[i]
        if i == i0 or month(c["t"]) != month(daily[i - 1]["t"]):
            b.cash += monthly
            b.cash_lots.append([i, monthly])
            b.contributed += monthly
            if model == "dca":
                b.buy_in(monthly, c["o"] if i > i0 else c["c"], c["t"], "monthly buy", split=False)
        if model == "dca":
            continue
        if i > i0:
            snap = b.orders()
            if is_a:
                for k, d in enumerate(DIP):
                    px = rungs["anchor"] * (1 - d / 100)
                    if not rungs["done"][k] and c["l"] <= px:
                        rungs["done"][k] = True
                        left = rungs["done"].count(False) + 1
                        b.buy_in(b.cash / left, px, c["t"], f"JUMP IN: dip rung {k + 1} (−{d}%)")
            if pending_b:
                b.buy_in(b.cash, c["o"], c["t"], "JUMP IN: washed out + turning up")
                pending_b = False
            old = sum(u for d0, u in b.cash_lots if i - d0 >= MAX_WAIT_DAYS)
            if old >= 1:
                b.buy_in(old, c["o"], c["t"], f"Waited {MAX_WAIT_DAYS} days: bought anyway")
            b.fill(snap, c)
        # ---- at the close: reset take-profits if trade coins were added, update signals
        if b.added_trade:
            b.reset_tp()
        if hasattr(b, "end_day"):
            b.end_day()
        if is_a:
            if all(rungs["done"]) or (any(rungs["done"]) and c["c"] > rungs["anchor"]):
                rungs = arm(i)
            elif not any(rungs["done"]):
                rungs = arm(i)          # nothing bought yet: the ladder trails the 30-day high
        else:
            if len(signals(i)[1]) >= need:
                washed.append(i)
            recent = any(i - w < WASHED_WINDOW for w in washed)
            crossed = i > 0 and c["c"] > sma20(daily, i) and daily[i - 1]["c"] <= sma20(daily, i - 1)
            if recent and crossed and b.cash >= 1 and i < i1:
                pending_b = True
    return b.result(daily[i1]["c"])


def compare(daily, i0, i1, signals):
    """All models over the window, plus each half run on its own (fresh start)."""
    mid = (i0 + i1) // 2
    out = {"start": day(daily[i0]["t"]), "end": day(daily[i1]["t"]), "days": i1 - i0 + 1,
           "monthly_usd": MONTHLY_USD, "core_share": CORE_SHARE,
           "results": {m: run(daily, i0, i1, signals, m) for m in MODELS},
           "halves": []}
    for a, z in ((i0, mid), (mid + 1, i1)):
        rs = {m: run(daily, a, z, signals, m) for m in MODELS}
        dca = rs["dca"]["coin_equiv"]
        out["halves"].append({"start": day(daily[a]["t"]), "end": day(daily[z]["t"]),
                              "vs_dca_pct": {m: round(100 * (r["coin_equiv"] / dca - 1), 2) for m, r in rs.items() if m != "dca"}})
    out["starts"] = []
    for k in range(STARTS):
        a = i0 + k * START_STEP_DAYS
        if k == 0 or i1 - a < 180:
            continue                        # k=0 is the full run; need 6+ months left
        rs = {m: run(daily, a, i1, signals, m) for m in MODELS}
        dca = rs["dca"]["coin_equiv"]
        out["starts"].append({"start": day(daily[a]["t"]),
                              "vs_dca_pct": {m: round(100 * (r["coin_equiv"] / dca - 1), 2) for m, r in rs.items() if m != "dca"}})
    return out


def overall(coins):
    rows = {}
    for c in coins:
        acc = c.get("accumulate")
        if not acc:
            continue
        dca = acc["results"]["dca"]["coin_equiv"]
        for m, r in acc["results"].items():
            if m == "dca":
                continue
            halves = [h["vs_dca_pct"][m] for h in acc["halves"]]
            starts = [r["coin_equiv"] / dca - 1] + [st["vs_dca_pct"][m] / 100 for st in acc.get("starts", [])]
            rows.setdefault(m, {"v": [], "halves_won": 0, "halves": 0, "starts_won": 0, "starts": 0, "all": []})
            rows[m]["starts_won"] += sum(1 for x in starts if x > 1e-9)
            rows[m]["starts"] += len(starts)
            rows[m]["all"] += starts
            rows[m]["v"].append(r["coin_equiv"] / dca - 1)
            rows[m]["halves_won"] += sum(1 for x in halves if x > 0)
            rows[m]["halves"] += len(halves)
    out = [{"strategy": m, "avg_vs_dca_pct": round(100 * sum(d["v"]) / len(d["v"]), 2),
            "worst_vs_dca_pct": round(100 * min(d["v"]), 2),
            "coins_beating_dca": sum(1 for x in d["v"] if x > 1e-9), "coins": len(d["v"]),
            "halves_beating_dca": d["halves_won"], "halves": d["halves"],
            "starts_beating_dca": d["starts_won"], "starts": d["starts"],
            "starts_worst_pct": round(100 * min(d["all"]), 2)} for m, d in rows.items()]
    return sorted(out, key=lambda r: (-r["coins_beating_dca"], -r["starts_beating_dca"], -r["halves_beating_dca"], -r["avg_vs_dca_pct"]))
