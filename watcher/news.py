"""News engine for Crypto Coach.

Reads a fixed list of news feeds, matches headlines to your coins with fixed
keyword rules, tags them (catalyst / hype / risk / macro / official), counts
mentions per coin per day, and decides flags 6 (news priced in) and
7 (social FOMO). No AI: the same input always gives the same output.
"""
import hashlib
import html
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

DAY = 86400

# ---------------------------------------------------------------- tag rules
# Matched against the HEADLINE only (headlines are where hype and catalysts show).
I = re.IGNORECASE
TAG_RULES = {
    "catalyst": [
        (r"\bpartner(s|ed|ship|ships)?\b", I), (r"\bETFs?\b", I), (r"\blist(s|ed|ing|ings)\b", I),
        (r"\bapprov(e|es|ed|al)\b", I), (r"\blaunch(es|ed)?\b", I), (r"\bmainnet\b", I),
        (r"\bupgrade[sd]?\b", I), (r"\bintegrat(e|es|ed|ion)\b", I), (r"\badopt(s|ed|ion)\b", I),
        (r"\bacqui(re|res|red|sition)\b", I), (r"\bfil(es|ed|ing) for\b", I), (r"\bpilot\b", I),
        (r"\btokeni[sz](e|es|ed|ation)\b", I), (r"\bdeal\b", I), (r"\bselect(s|ed)\b", I),
    ],
    "hype": [
        (r"price prediction", I), (r"\bsoar(s|ed|ing)?\b", I), (r"\bskyrocket(s|ed|ing)?\b", I),
        (r"\bexplod(e|es|ed|ing)\b", I), (r"\bsurg(e|es|ed|ing)\b", I), (r"\brall(y|ies|ied|ying)\b", I),
        (r"\ball[- ]time high", I), (r"\bparabolic\b", I), (r"\bbreakout\b", I), (r"\bbull run\b", I),
        (r"\bfomo\b", I), (r"\b(10|20|50|100|1000)x\b", I), (r"\bmoon(s|ing)?\b", I),
        (r"\bpump(s|ed|ing)?\b", I), (r"\bcould (hit|reach)\b", I), (r"\bnext target\b", I),
        (r"\bjump(s|ed)?\b", I), (r"\brocket(s|ed|ing)?\b", I),
    ],
    "risk": [
        (r"\bhack(s|ed)?\b", I), (r"\bexploit(s|ed)?\b", I), (r"\boutage\b", I), (r"\blawsuit\b", I),
        (r"\bsue[sd]?\b", I), (r"\bdelist(s|ed|ing)?\b", I), (r"\bcrash(es|ed)?\b", I),
        (r"\bplung(e|es|ed)\b", I), (r"\bliquidat(e|ed|ion|ions)\b", I), (r"\bscam\b", I),
        (r"\binvestigat(e|es|ed|ion)\b", I), (r"\bprob(e|es|ed|ing)\b", I),
    ],
    "macro": [
        (r"\bFed\b", 0), (r"\bFOMC\b", 0), (r"\brate (cut|hike)s?\b", I), (r"\binterest rates?\b", I),
        (r"\binflation\b", I), (r"\bCPI\b", 0), (r"\bjobs report\b", I), (r"\bTreasur(y|ies)\b", I),
        (r"\btariffs?\b", I), (r"\brecession\b", I), (r"\bPowell\b", 0),
    ],
}
_TAG_RX = {k: [re.compile(p, f) for p, f in v] for k, v in TAG_RULES.items()}


def tag_headline(title):
    return [k for k, rxs in _TAG_RX.items() if any(rx.search(title) for rx in rxs)]


def keyword_rx(word):
    """Whole-word match. ALL-CAPS tickers are case-sensitive (so 'sol' or 'Sol'
    in normal text don't count as SOL); names are case-insensitive."""
    flags = 0 if (word.isupper() and len(word) <= 6) else re.IGNORECASE
    return re.compile(r"(?<![A-Za-z0-9])" + re.escape(word) + r"(?![A-Za-z0-9])", flags)


def coin_keywords(coin):
    words = coin.get("keywords") or [coin.get("name", ""), coin["symbol"]]
    return [keyword_rx(w) for w in words if w]


# ---------------------------------------------------------------- parsing
def _local(tag):
    return tag.rsplit("}", 1)[-1]


def _child(el, *names):
    for c in el:
        if _local(c.tag) in names:
            return c
    return None


def _text(el):
    return "" if el is None else "".join(el.itertext()).strip()


def clean(s, limit=400):
    s = html.unescape(re.sub(r"<[^>]+>", " ", html.unescape(s or "")))
    return re.sub(r"\s+", " ", s).strip()[:limit]


def parse_date(s):
    s = (s or "").strip()
    if not s:
        return None
    try:
        d = parsedate_to_datetime(s)
    except (TypeError, ValueError, IndexError):
        try:
            d = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return int(d.timestamp())


def parse_feed(data):
    """RSS 2.0, Atom, or Google news-sitemap -> [{title, link, ts, summary}]."""
    root = ET.fromstring(data)
    out = []
    for el in root.iter():
        kind = _local(el.tag)
        if kind == "item":  # RSS
            title, link = _text(_child(el, "title")), _text(_child(el, "link"))
            ts = parse_date(_text(_child(el, "pubDate", "date")))
            summary = _text(_child(el, "description", "summary"))
        elif kind == "entry":  # Atom
            title = _text(_child(el, "title"))
            link = ""
            for c in el:
                if _local(c.tag) == "link" and c.get("rel", "alternate") == "alternate":
                    link = c.get("href", "")
                    break
            ts = parse_date(_text(_child(el, "published", "updated")))
            summary = _text(_child(el, "summary", "content"))
        elif kind == "url":  # news sitemap
            news = _child(el, "news")
            if news is None:
                continue
            title = _text(_child(news, "title"))
            link = _text(_child(el, "loc"))
            ts = parse_date(_text(_child(news, "publication_date")))
            summary = ""
        else:
            continue
        title = clean(title, 300)
        if title and ts:
            link = link.strip()
            if not link.lower().startswith(("http://", "https://")):
                link = ""  # never pass through javascript:, data: or relative links
            out.append({"title": title, "link": link, "ts": ts, "summary": clean(summary)})
    return out


# ---------------------------------------------------------------- state + metrics
def day_of(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def day_shift(day, n):
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=n)
    return d.strftime("%Y-%m-%d")


def item_id(it):
    return hashlib.sha1((it.get("link") or it["title"]).encode()).hexdigest()[:12]


def update_state(state, fetched, coins, now):
    """fetched: list of (source dict, [items]). Mutates and returns state."""
    st = state or {}
    st.setdefault("items", [])
    st.setdefault("seen", {})
    st.setdefault("daily", {})
    st.setdefault("since", {})
    today = day_of(now)
    kws = {c["symbol"]: coin_keywords(c) for c in coins}
    for c in coins:
        st["since"].setdefault(c["symbol"], today)
        st["daily"].setdefault(c["symbol"], {})

    for src, items in fetched:
        for it in items:
            h = item_id(it)
            if h in st["seen"]:
                continue
            ts = min(it["ts"], now)  # never count the future
            st["seen"][h] = day_of(ts)
            if ts < now - 7 * DAY:
                continue
            text = it["title"] + " " + it.get("summary", "")
            if src.get("coin"):
                matched = [src["coin"]] if src["coin"] in kws else []
            else:
                matched = [sym for sym, rxs in kws.items() if any(rx.search(text) for rx in rxs)]
            tags = tag_headline(it["title"])
            if src.get("coin"):
                tags = ["official"] + tags
            for sym in matched:
                d = st["daily"][sym]
                d[day_of(ts)] = d.get(day_of(ts), 0) + 1
            if matched or "macro" in tags:
                st["items"].append({"id": h, "title": it["title"], "link": it["link"], "ts": ts,
                                    "source": src["name"], "coins": matched, "tags": tags})

    # prune
    keep_seen = day_shift(today, -21)
    st["seen"] = {h: d for h, d in st["seen"].items() if d >= keep_seen}
    keep_day = day_shift(today, -35)
    for sym in st["daily"]:
        st["daily"][sym] = {d: n for d, n in st["daily"][sym].items() if d >= keep_day}
    st["items"] = sorted([i for i in st["items"] if i["ts"] >= now - 7 * DAY], key=lambda i: -i["ts"])[:400]
    return st


def coin_metrics(st, sym, now, th, trending_rank):
    today = day_of(now)
    daily = st["daily"].get(sym, {})
    m3 = sum(daily.get(day_shift(today, -k), 0) for k in range(3))
    since = st["since"].get(sym, today)
    first = max(since, day_shift(today, -30))
    last = day_shift(today, -3)
    base_days, d = [], first
    while d <= last:
        base_days.append(daily.get(d, 0))
        d = day_shift(d, 1)
    n_base = len(base_days)
    avg = (sum(base_days) / n_base) if n_base else None
    if n_base >= 7:
        ratio = (m3 / 3) / max(avg, 1 / 3)
        spike = ratio >= th["news_spike_x"] and m3 >= th["news_min_mentions_3d"]
    else:
        ratio = None
        spike = m3 >= th["news_cold_start_mentions_3d"]
    mine = [i for i in st["items"] if sym in i["coins"]]
    catalysts = [i for i in mine if i["ts"] >= now - 7 * DAY and ("catalyst" in i["tags"] or "official" in i["tags"])]
    hype3 = [i for i in mine if i["ts"] >= now - 3 * DAY and "hype" in i["tags"]]
    return {
        "mentions_3d": m3, "baseline_per_day": None if avg is None else round(avg, 3),
        "baseline_days": n_base, "spike_ratio": None if ratio is None else round(ratio, 2), "spike": spike,
        "catalysts_7d": len(catalysts), "catalyst_titles": [i["title"] for i in catalysts[:3]],
        "hype_3d": len(hype3), "hype_titles": [i["title"] for i in hype3[:3]],
        "trending_rank": trending_rank,
        "daily14": [[day_shift(today, -k), daily.get(day_shift(today, -k), 0)] for k in range(13, -1, -1)],
    }


def news_flags(m, ch7, th, news_ok, trending_ok):
    up7 = ch7 if ch7 is not None else 0
    on6 = news_ok and up7 >= th["news_move_7d_pct"] and m["spike"] and m["catalysts_7d"] >= 1
    if not news_ok:
        d6 = "news feeds unavailable this run"
    else:
        sp = (f"{m['spike_ratio']}× normal" if m["spike_ratio"] is not None
              else f"building baseline ({m['baseline_days']}/7 days)")
        d6 = f"7d {up7:+.1f}%, {m['mentions_3d']} mentions in 3d ({sp}), {m['catalysts_7d']} catalyst headlines"
    trend = m["trending_rank"] is not None
    on7 = up7 >= th["fomo_move_7d_pct"] and (trend or (news_ok and m["hype_3d"] >= th["fomo_hype_3d"]))
    tr = (f"#{m['trending_rank']} trending on CoinGecko" if trend
          else ("not trending" if trending_ok else "trending list unavailable"))
    d7 = f"7d {up7:+.1f}%, {tr}, {m['hype_3d']} hype headlines in 3d"
    return {"news": {"on": bool(on6), "detail": d6}, "fomo": {"on": bool(on7), "detail": d7}}
