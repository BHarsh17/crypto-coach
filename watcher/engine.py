"""Crypto Coach v4 rules engine: price levels only, so it can run on public data.

Per coin it keeps three kinds of orders (the same ones you place on Kraken):
  * take-profits (JUMP OUT): sell part of your trade bucket at +20% and +40% above the reference price
  * buy-backs (JUMP IN): after each take-profit, buy back with that sale's money at 20% and 40%
    below the sale price
  * dip ladder (JUMP IN): spend your dip reserve at -10/-25/-40/-55% from the ladder's start

The reference price starts as your average cost (coins.json "cost", or the first-run price).
After a buy-back fills, a new cycle starts and the take-profits are measured from what you
paid to buy back. Dip-ladder buys don't move the reference.

Fill rules (same as limit orders): a sell fills if the high reaches it, a buy if the low reaches it.
An order created during a step can't fill until the next step, because a candle doesn't show
whether its high or its low came first.

Amounts (how many coins, how many dollars) are NOT here: they depend on your holdings, which
stay private in your browser. Shares are percentages the app turns into amounts.
"""

DEFAULT_RULES = {
    "take_profits": [20, 40],          # % above the reference price
    "take_profit_share": 50,           # % of the trade bucket (at the start of the cycle) each one sells
    "buy_backs": [20, 40],             # % below each sale price
    "dip_ladder": [{"drop": 10, "share": 25}, {"drop": 25, "share": 25},
                   {"drop": 40, "share": 25}, {"drop": 55, "share": 25}],
}


def rules_from(cfg):
    r = dict(DEFAULT_RULES)
    r.update({k: v for k, v in (cfg or {}).items() if k in DEFAULT_RULES})
    return r


def new_state(cost, anchor, rules):
    return {"cycle": 1, "ref": cost, "cost_set": cost, "tp_done": [False] * len(rules["take_profits"]),
            "anchor": anchor, "dip_done": [False] * len(rules["dip_ladder"]),
            "buyback_sets": [], "cycle_buys": [], "events": [], "next_id": 1}


def sync_config(st, cost, anchor, rules):
    """You changed cost or anchor in coins.json -> restart that part."""
    if cost is not None and abs(cost - st.get("cost_set", cost)) > 1e-12:
        st.update(ref=cost, cost_set=cost, tp_done=[False] * len(rules["take_profits"]), cycle_buys=[])
        st["cycle"] += 1
    if anchor is not None and abs(anchor - st["anchor"]) > 1e-12:
        st.update(anchor=anchor, dip_done=[False] * len(rules["dip_ladder"]))
    if len(st["tp_done"]) != len(rules["take_profits"]):
        st["tp_done"] = [False] * len(rules["take_profits"])
    if len(st["dip_done"]) != len(rules["dip_ladder"]):
        st["dip_done"] = [False] * len(rules["dip_ladder"])
    return st


def tp_price(st, rules, k):
    return st["ref"] * (1 + rules["take_profits"][k] / 100)


def dip_price(st, rules, k):
    return st["anchor"] * (1 - rules["dip_ladder"][k]["drop"] / 100)


def _event(st, ts, kind, price, **kw):
    e = {"id": st["next_id"], "ts": ts, "kind": kind, "price": price, "cycle": st["cycle"]}
    e.update(kw)
    st["next_id"] += 1
    st["events"].append(e)
    st["events"] = st["events"][-60:]
    return e


def step(st, high, low, ts, rules):
    """Apply one period's high/low. Returns the new events (also stored in st['events'])."""
    new = []
    created = []   # buy-back sets made during this step: not fillable until the next one
    reset_this_step = False

    # 1) buys (lows): dip ladder, then buy-backs
    for k, done in enumerate(st["dip_done"]):
        px = dip_price(st, rules, k)
        if not done and low <= px:
            st["dip_done"][k] = True
            new.append(_event(st, ts, "dip_buy", px, rung=k + 1, share=rules["dip_ladder"][k]["share"],
                              drop=rules["dip_ladder"][k]["drop"]))
    for bs in st["buyback_sets"]:
        for lv in bs["levels"]:
            if not lv["filled"] and low <= lv["price"]:
                lv["filled"] = True
                if not st["cycle_buys"]:          # first buy-back of a new cycle
                    st["cycle"] += 1
                    st["tp_done"] = [False] * len(rules["take_profits"])
                reset_this_step = True            # take-profit prices just moved: they wait for the next step
                st["cycle_buys"].append(lv["price"])
                # reference = average price paid, equal-dollar buys -> harmonic mean
                st["ref"] = len(st["cycle_buys"]) / sum(1 / p for p in st["cycle_buys"])
                new.append(_event(st, ts, "buy_back", lv["price"], sale_id=bs["sale_id"],
                                  sale_price=bs["sale_price"], level=lv["n"], share=lv["share"], drop=lv["drop"]))
    st["buyback_sets"] = [b for b in st["buyback_sets"] if not all(lv["filled"] for lv in b["levels"])]

    # 2) sells (highs): take-profits. If any buy-back filled, the take-profit prices just moved
    #    (new cycle or new average), so those orders only exist from now on: they wait a step.
    if reset_this_step:
        st["buyback_sets"] += created
        return new
    for k, done in enumerate(st["tp_done"]):
        px = tp_price(st, rules, k)
        if not done and high >= px:
            st["tp_done"][k] = True
            st["cycle_buys"] = []                 # the next buy-back starts a new cycle
            e = _event(st, ts, "take_profit", px, level=k + 1, pct=rules["take_profits"][k],
                       share=rules["take_profit_share"])
            new.append(e)
            n = len(rules["buy_backs"])
            created.append({"sale_id": e["id"], "sale_price": px, "levels": [
                {"n": j + 1, "price": px * (1 - d / 100), "drop": d, "share": round(100 / n, 6), "filled": False}
                for j, d in enumerate(rules["buy_backs"])]})
    st["buyback_sets"] += created
    return new


def open_orders(st, rules):
    """Everything that should be sitting on the exchange right now."""
    out = []
    for k, done in enumerate(st["tp_done"]):
        if not done:
            out.append({"kind": "take_profit", "price": tp_price(st, rules, k), "level": k + 1,
                        "pct": rules["take_profits"][k], "share": rules["take_profit_share"]})
    for bs in st["buyback_sets"]:
        for lv in bs["levels"]:
            if not lv["filled"]:
                out.append({"kind": "buy_back", "price": lv["price"], "sale_id": bs["sale_id"],
                            "sale_price": bs["sale_price"], "level": lv["n"], "drop": lv["drop"], "share": lv["share"]})
    for k, done in enumerate(st["dip_done"]):
        if not done:
            out.append({"kind": "dip_buy", "price": dip_price(st, rules, k), "rung": k + 1,
                        "drop": rules["dip_ladder"][k]["drop"], "share": rules["dip_ladder"][k]["share"]})
    return out
