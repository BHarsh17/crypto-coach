"""v6 market radar: pivots and level breaks, the pump & dump meter, news tone, Fear & Greed,
and the alerts built from them. Pure functions; watch.py feeds them Kraken candles.

Nothing here trades. It tells you what's happening:
  - Monthly Traditional pivots (as on TradingView): P, R1, R2, S1, S2 from last month's H/L/C.
  - A level is "broken" when a completed 4-hour candle CLOSES beyond it after the previous one
    closed on the other side. Each level/direction alerts once per month.
  - Pump meter 0-100 from speed, stretch, RSI, volume surge, leverage, news spike and trending.
  - Dump warning: a coin that pumped (7-day gain of 40%+ in the last 30 days) and is now 20%+
    below the peak it made since.
"""
from datetime import datetime, timezone

DAY = 86400


def month_of(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m")


def pivots(daily, now=None):
    """This month's Traditional pivots from the previous calendar month's daily candles."""
    if not daily:
        return None
    cur = month_of(now if now is not None else daily[-1]["t"])
    months = sorted({month_of(c["t"]) for c in daily if month_of(c["t"]) < cur})
    if not months:
        return None
    prev = [c for c in daily if month_of(c["t"]) == months[-1]]
    h, l, c = max(x["h"] for x in prev), min(x["l"] for x in prev), prev[-1]["c"]
    p = (h + l + c) / 3
    return {"month": cur, "from": months[-1], "P": p, "R1": 2 * p - l, "S1": 2 * p - h,
            "R2": p + (h - l), "S2": p - (h - l)}


LEVEL_ORDER = ("S2", "S1", "P", "R1", "R2")


def level_breaks(h4, piv, since_ts, done):
    """New closes beyond a pivot level in completed 4h candles that closed after since_ts.
    done: set of already-alerted keys for this month like 'S1:down'. Returns [(key, level, price, close, ts)]."""
    if not piv or len(h4) < 3:
        return []
    out = []
    complete = h4[:-1]                                  # the last candle is still forming
    for a, b in zip(complete, complete[1:]):
        if b["t"] + 4 * 3600 <= since_ts or month_of(b["t"]) != piv["month"]:
            continue
        for name in LEVEL_ORDER:
            lv = piv[name]
            for direction, crossed in (("down", a["c"] >= lv > b["c"]), ("up", a["c"] <= lv < b["c"])):
                key = f"{name}:{direction}"
                if crossed and key not in done:
                    done.add(key)
                    out.append((key, name, lv, b["c"], b["t"]))
    return out


def clamp(x, a=0.0, b=1.0):
    return max(a, min(b, x))


def pump_meter(ch7, ch30, price, ma20, ma50, rsi_d, vol_ratio, funding_8h, oi_growth, news_spike, hype3, trending_rank):
    """0-100. Each part scores 0..1, then weights. Returns (score, label, parts)."""
    parts = {
        "speed": max(clamp((ch7 or 0) / 60), clamp((ch30 or 0) / 150)),                     # +60%/7d or +150%/30d = max
        "stretch": max(clamp(((price / ma20 - 1) * 100 if ma20 else 0) / 35),
                       clamp(((price / ma50 - 1) * 100 if ma50 else 0) / 50)),
        "rsi": clamp(((rsi_d or 50) - 60) / 25),                                             # 60 -> 0, 85 -> 1
        "volume": clamp(((vol_ratio or 1) - 1) / 2),                                         # 3x normal = max
        "leverage": max(clamp((funding_8h or 0) / 0.0005), clamp((oi_growth or 0) / 30)),
        "news": max(1.0 if news_spike else 0.0, clamp((hype3 or 0) / 3)),
        "trending": 1.0 if trending_rank else 0.0,
    }
    w = {"speed": 30, "stretch": 20, "rsi": 15, "volume": 10, "leverage": 10, "news": 10, "trending": 5}
    score = round(sum(parts[k] * w[k] for k in w))
    label = "Extreme" if score >= 80 else "Pumping" if score >= 60 else "Heating" if score >= 30 else "Calm"
    return score, label, {k: round(v, 2) for k, v in parts.items()}


def dump_watch(daily):
    """Did this coin pump (7-day gain >= 40%) in the last 30 days, and is it now 20%+ below the peak since?"""
    if len(daily) < 40:
        return {"on": False}
    last = daily[-30:]
    pump_i = None
    for i in range(len(daily) - 30, len(daily)):
        if i >= 7 and daily[i]["c"] / daily[i - 7]["c"] - 1 >= 0.40:
            pump_i = i
    if pump_i is None:
        return {"on": False}
    peak = max(c["h"] for c in daily[pump_i - 7:])
    price = daily[-1]["c"]
    off = price / peak - 1
    return {"on": off <= -0.20, "pumped": True, "peak": peak, "off_peak_pct": round(off * 100, 1),
            "pump_day": datetime.fromtimestamp(daily[pump_i]["t"], timezone.utc).strftime("%Y-%m-%d")}


def news_tone(n_positive, n_risk):
    """Per-coin headline tone over 7 days."""
    total = n_positive + n_risk
    if total < 2:
        return {"label": "Quiet", "score": 0}
    score = round((n_positive - n_risk) / total * 100)
    label = "Optimistic" if score >= 30 else "Pessimistic" if score <= -30 else "Mixed"
    return {"label": label, "score": score}


def parse_fear_greed(doc):
    """alternative.me /fng/ JSON -> {"value", "label", "week_ago"}."""
    rows = (doc or {}).get("data") or []
    if not rows:
        return None
    v = int(rows[0]["value"])
    wk = int(rows[7]["value"]) if len(rows) > 7 else None
    return {"value": v, "label": rows[0].get("value_classification", ""), "week_ago": wk}


def take_profit_window(price, ch7, ma20, rsi_d, piv, fear_greed, pump, news, funding_hot, trending_rank, flags_on=0):
    """Is this a reasonable moment to consider trimming? Returns {"on", "strength", "reasons", "score"}.
    On when the pump meter is 70+ or 3+ independent reasons line up. Information only."""
    r = []
    if pump and pump.get("score", 0) >= 60:
        r.append(f"pump meter {pump['score']}/100 ({pump['label']})")
    if (ch7 or 0) >= 25:
        r.append(f"{ch7:+.0f}% in 7 days")
    if ma20 and price / ma20 - 1 >= 0.25:
        r.append(f"{(price / ma20 - 1) * 100:.0f}% above its 20-day average")
    if (rsi_d or 0) >= 75:
        r.append(f"RSI {rsi_d:.0f} (overbought)")
    if piv:
        if price > piv["R2"]:
            r.append(f"above monthly R2 ({piv['R2']:.4g})")
        elif price > piv["R1"]:
            r.append(f"above monthly R1 ({piv['R1']:.4g})")
    if fear_greed and fear_greed.get("ok") and fear_greed.get("value", 0) >= 75:
        r.append(f"market mood {fear_greed['value']}/100 ({fear_greed.get('label', 'Greed')})")
    n = news or {}
    if n.get("spike") or (n.get("hype_3d") or 0) >= 3:
        r.append("news hype spiking")
    if trending_rank:
        r.append(f"trending on CoinGecko #{trending_rank}")
    if funding_hot:
        r.append("leverage piling in")
    if flags_on >= 3:
        r.append(f"{flags_on}/6 top-warning flags")
    score = pump.get("score", 0) if pump else 0
    on = score >= 70 or len(r) >= 3
    strength = "Strong" if on and (score >= 80 or len(r) >= 5) else "Possible" if on else "No"
    return {"on": on, "strength": strength, "reasons": r}
