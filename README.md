# Crypto Coach

**Version:** see `VERSION` (currently 3.0.0) · **What changed:** see `CHANGELOG.md`. The app shows its version and the watcher's in its footer, and warns if they differ.

**Your rules, not your gut.** Crypto Coach watches your coins, checks them against rules you set ahead of time, and tells you what those rules say to do: hold, buy a dip rung, trim a pump, or follow a stop. You still make every trade. It just makes sure the plan gets followed instead of the panic.

> These are rule-based signals, not predictions or financial advice. They can't promise what the market will do. They make sure *your* plan gets followed.

---

## Contents
1. [The big picture](#1-the-big-picture)
2. [What happens on GitHub](#2-what-happens-on-github)
3. [What happens in the app](#3-what-happens-in-the-app)
4. [The 6 top-warning flags](#4-the-6-top-warning-flags)
5. [The 6 bottom signals](#5-the-6-bottom-signals)
6. [The dip ladder](#6-the-dip-ladder)
7. [Buckets, trims and the trailing stop](#7-buckets-trims-and-the-trailing-stop)
8. [How a coin's mission is chosen](#8-how-a-coins-mission-is-chosen)
9. [Telegram alerts](#9-telegram-alerts)
   - [The News tab](#the-news-tab)
10. [Trust guards: how it avoids false info](#10-trust-guards-how-it-avoids-false-info)
11. [The game: XP and levels](#11-the-game-xp-and-levels)
12. [What's public, what's private](#12-whats-public-whats-private)
13. [Changing things](#13-changing-things)
14. [Setup (first time)](#14-setup-first-time)
15. [Troubleshooting](#15-troubleshooting)

---

## 1. The big picture

There are two parts. One does the thinking, the other shows it to you.

```
┌──────────────── GITHUB (cloud, runs even when your PC is off) ───────────────┐
│                                                                               │
│  Every 15 min ─► tests/test_watch.py   checks the maths. Fail = stop.         │
│                  watcher/watch.py      pulls Kraken data, scores every coin   │
│                  watcher/news.py       reads 7 fixed news feeds + CoinGecko   │
│                                        trending, sets the news/FOMO flags     │
│                        │                                                      │
│                        ├─► Telegram alert (only when something changes)       │
│                        └─► publishes to the website (GitHub Pages):           │
│                              signals.json        flags, ladder, signals, news │
│                              candles/ETH.json…   chart history per coin       │
└───────────────────────────────────────┬───────────────────────────────────────┘
                                        │
                    https://bharsh17.github.io/crypto-coach/
                                        │
┌───────────────────────────── THE APP (your PC / phone) ──────────────────────┐
│  reads signals.json every 5 min  ─►  rules, flags, ladder, missions          │
│  live Kraken WebSocket feed      ─►  prices + candles tick in real time      │
│  your browser storage            ─►  holdings, reserve, XP, journal (private)│
└───────────────────────────────────────────────────────────────────────────────┘
```

**The repo is the recipe, the website is the meal.** The repo holds the code and your settings (`coins.json`). The data files are never saved in the repo. Each run publishes them straight to the website, which is why you won't see them in the file list.

### How often things update

| What | How often | Source |
|---|---|---|
| Prices, card charts, big chart | **Instantly**, on every trade | Kraken live feed (app) |
| Missions on each card | Re-checked every **20 s** with the live price | App |
| Flags, RSI, averages, bottom signals, news | App re-reads every **5 min** | Watcher's `signals.json` |
| The watcher | Every **15 min** (GitHub may start it a few minutes late) | GitHub Actions |
| Telegram alerts | Only when a rule changes | Watcher |

**Why the flags aren't real-time:** every flag is built on daily or weekly candles, so they barely change minute to minute. Checking faster would add noise, not information.

---

## 2. What happens on GitHub

Each run of the workflow (`.github/workflows/watch.yml`) does this, in order:

1. **Self-test** (`tests/test_watch.py`, 30 checks in three layers):
   - **Reference values:** RSI against a published worked example and a hand-worked one, plus averages, percentages and funding maths.
   - **Cross-checks:** a second, independently written RSI and average compared on hundreds of random price series. They must agree to 8 decimal places.
   - **Scenarios:** every flag, bottom signal, ladder rung and alert tested right at its on/off boundary, the news counting and hype maths, and two full offline runs back to back (checking nothing is double-counted).

   If any check fails, the run stops and **nothing is published**, so the app keeps the last good data.
2. **Read the previous results** from the website. This is the watcher's memory: ladder anchors, which rungs were already reached, open-interest history, and which alerts were already sent.
3. **For each coin in `coins.json`:**
   - Download candles from Kraken's free public API: **1-hour, 4-hour, daily, weekly**.
   - **Validate** them (see [Trust guards](#10-trust-guards-how-it-avoids-false-info)). Bad data is rejected, not used.
   - Download **funding rate** and **open interest** from Kraken Futures, if that coin has a futures market.
   - Calculate the indicators, then flags 1–4, the bottom signals and the ladder.
   - Write the coin's chart file (`candles/SYMBOL.json`).
4. **News step:** read the 7 fixed news feeds and CoinGecko's trending list, match headlines to your coins, update the hype meter, and set flags 5–6 (news priced in, social FOMO).
5. **Send Telegram alerts** for anything that changed since last run.
6. **Publish** `index.html` (the app) + `signals.json` + `candles/` to GitHub Pages.

A run takes about 30 seconds. On a public repo it's free with no limits.

### The indicators it calculates

| Indicator | Meaning | How |
|---|---|---|
| **Price** | Latest trade price | Close of the current daily candle |
| **24h / 7d / 30d change** | How far it moved | Price vs. the daily close 1, 7 and 30 days ago |
| **20-, 50-, 200-day average** | The "normal" level | Simple average of the last 20/50/200 daily closes |
| **200-week average** | Long-cycle floor | Simple average of the last 200 weekly closes |
| **Daily / weekly RSI** | How overbought (high) or oversold (low), 0–100 | Wilder's 14-period RSI (a completely flat series reads 50) |
| **90-day high** | Recent peak | Highest daily high in 90 days |
| **2-year high / drawdown** | How far below the high | Highest high in Kraken's ~2 years of daily data |
| **Funding rate** | What leveraged longs pay shorts | Average of the last 8 Kraken Futures funding periods, converted to a **per-8-hour** rate using the actual spacing between periods |
| **Open interest** | Total open futures bets | Kraken Futures ticker, sampled once a day |
| **News mentions** | How much the coin is in the news | Headlines from the fixed outlets that match the coin's keywords, counted per day |
| **Trending rank** | How much people are searching for it | Position in CoinGecko's top-15 trending searches (last 24h) |

---

## 3. What happens in the app

The app is one web page (`site/index.html`). Open it at **https://bharsh17.github.io/crypto-coach/** on your PC or phone. On your phone, use *Add to Home Screen*.

- **Missions tab:** one card per coin, showing the live price, a chart, **one mission** (what your rules say right now), the flags, the bottom signals, the dip ladder and your buckets.
- **News tab:** the hype meter and every headline the watcher matched to your coins (see [The News tab](#the-news-tab)).
- **Dip reserve tab:** your cash pool for buying dips, how much each coin has used, and a **list of limit orders to place** on your exchange.
- **Journal tab:** how XP works, then everything you've logged, with XP.
- **Setup tab:** your holdings, average cost, dip reserve and backup.
- **😰 I want to sell:** shows your rules and offers a 24-hour cooldown instead of a panic sale.
- **Charts:** tap a card's chart or 📈 Chart. You get live candles with the 20/50/200 averages, ladder rungs, trailing stop and your average cost drawn on. **Make decisions on 1D, check the big picture on 1W.** 1H and 4H are for watching, not acting.

### The live dots
- **Top right "● live" (green):** the Kraken feed is connected and sending data.
- **"● live" on a card (green):** that coin's price is live. **"watcher"** means it's showing the watcher's price, either because the feed is down or because the live price failed a safety check.

---

## 4. The 6 top-warning flags

The flags warn that a coin **may be topping out**, the "too good to be true" moment, like QNT going from about $65 to $372 in late September 2026. Each flag is either **on** (red dot) or **off**. The count, out of 6, decides whether your trim rule kicks in.

**All 6 are set automatically by the watcher**, with fixed rules, every 15 minutes. Flags 1–4 come from price and futures data; flags 5–6 come from fixed news outlets and CoinGecko's trending list.

The numbers below are the defaults from `coins.json` → `thresholds`. You can change them.

### Flag 1 · Vertical move 🚀
**What it means:** the price shot up far too fast. Moves like this rarely hold.
**ON when:** the price is up **≥ 60% in 7 days** *or* **≥ 150% in 30 days**.
**OFF when:** neither is true. It turns off by itself as the fast move ages out of the 7- and 30-day windows.
*Settings: `vertical_7d_pct`, `vertical_30d_pct`*

### Flag 2 · Stretched 🪢
**What it means:** the price is far above its normal level, like a stretched rubber band. Prices usually snap back toward the average.
**ON when:** the price is **≥ 50% above the 50-day average** *or* **≥ 35% above the 20-day average**.
**OFF when:** the price falls back toward the averages, or the averages rise to catch up.
*Settings: `stretched_ma50_pct`, `stretched_ma20_pct`*

### Flag 3 · Extreme RSI 🌡️
**What it means:** buying has been one-sided for too long, so it's overbought.
**ON when:** **daily RSI ≥ 85** *or* **weekly RSI ≥ 80**.
**OFF when:** both drop below those levels.
*Settings: `rsi_daily_hot`, `rsi_weekly_hot`*

### Flag 4 · Leverage piling in 🎰
**What it means:** traders are borrowing to chase the move. If the price dips, their positions get force-closed, and that selling cascades.
**ON when either:**
- the **funding rate ≥ 0.05% per 8 hours** (about 5× the normal 0.01%: longs paying a lot to stay in), *or*
- **open interest is up ≥ 30% versus a sample at least 6 days old** *and* **the price is up ≥ 15% in 7 days**.

**OFF when:** neither is true.
**Notes:** coins with no Kraken futures market (shown as "no futures data") never turn this on. The open-interest part only starts working once the watcher has a sample that's 6+ days old, so for the first week after setup (or after adding a coin) only the funding part can fire.
*Settings: `funding_hot_8h`, `oi_growth_7d_pct`*

### Flag 5 · News priced in 📰
**What it means:** the catalyst is real (a partnership, an ETF, a listing, an upgrade), but the whole move happened in days. That's the classic "buy the rumour, sell the news" setup.
**ON when all three are true:**
1. the price is up **≥ 25% in 7 days**, *and*
2. the coin's **news mentions are spiking**: in the last 3 days it got **≥ 3× its normal daily mentions** *and* **at least 5 mentions**, *and*
3. there's **at least 1 catalyst headline** (or an official-source post) about it in the last 7 days.

**"Normal" mentions** = the coin's average mentions per day from 3 to 30 days ago. A coin that's normally silent is treated as having at least 1 mention every 3 days, so a jump from 0 to 1 doesn't count as a spike. **During the first 7 days** of tracking, there isn't enough history for a "normal" yet, so the spike rule is simply **8+ mentions in 3 days**.
**OFF when:** any of the three stops being true.
*Settings: `news_move_7d_pct`, `news_spike_x`, `news_min_mentions_3d`, `news_cold_start_mentions_3d`*

### Flag 6 · Social FOMO 📣
**What it means:** everyone is searching for it and the headlines are euphoric. When everyone who wants in is already in, there are no buyers left.
**ON when** the price is up **≥ 15% in 7 days** *and either*:
- the coin is in **CoinGecko's top-15 trending searches**, *or*
- there are **≥ 3 hype headlines** about it in the last 3 days ("soars", "price prediction", "could hit $…", "all-time high"…).

The price condition is there so a coin trending because it *crashed* never counts as FOMO.
**OFF when:** the price condition fails, or it's neither trending nor hyped.
*Settings: `fomo_move_7d_pct`, `fomo_hype_3d`*

### Overriding flags 5–6
Tap the **News priced in** or **Social FOMO** chip on a coin card to override it if you think the rule got it wrong. Each tap cycles **automatic → forced ON → forced OFF → automatic**. Overrides last **7 days** and are stored in your browser only. Flags 1–4 can't be overridden, because they're pure maths.

### What the count means

| Flags on | Level | Your rule |
|---|---|---|
| 0–2 | — | Hold. Watch it, don't act. |
| **3** | Level 1 | Trim **25%** of the trade bucket |
| **4–5** | Level 2 | Trim to **50%** total |
| **6** | Level 3 | Trim to **75%** total. The last 25% is your **moonbag**, protected by the trailing stop. |

### Top flags vs. bottom signals
There are 6 of each, built as mirror images, with the same 3 / 4–5 / 6 levels. Buying never *waits* for bottom signals: your core buys run on a schedule and ladder rungs fill on price alone. Bottom signals only tell you to buy the next rung **early**.

**Top flags = trim (sell part of the trade bucket). Bottom signals = buy the next ladder rung early.** Neither ever touches your core.

## 5. The 6 bottom signals

These are the buy-side mirror of the top flags. Each one lights up (blue dot) when a coin looks **washed out**, and each matches a top flag pointing the other way:

| Bottom signal | ON when | Mirror of |
|---|---|---|
| **Freefall** 🪂 | down **≥ 25% in 7 days** *or* **≥ 45% in 30 days** | Vertical move |
| **Stretched below** 🪢 | **≥ 25% below the 50-day average** *or* **≥ 20% below the 20-day average** | Stretched |
| **Oversold RSI** 🥶 | **daily RSI ≤ 25** *or* **weekly RSI ≤ 30** | Extreme RSI |
| **Shorts crowded** 🐻 | **funding ≤ −0.01% per 8 hours** (traders paying to bet on a fall; crowded shorts often get squeezed) | Leverage piling in |
| **Capitulation volume** 🏳️ | in any of the **last 3 completed days**, a day closed **≥ 5% below its open** on **≥ 2.5× the 30-day average volume** | News priced in / FOMO (the panic version) |
| **Near 200-week average** 🧊 | price **at or below 10% above the 200-week average** (past bear markets bottomed near it) | — (long-cycle floor) |

*Settings: `freefall_7d_pct`, `freefall_30d_pct`, `stretched_down_ma50_pct`, `stretched_down_ma20_pct`, `rsi_daily_cold`, `rsi_weekly_cold`, `funding_cold_8h`, `capitulation_volume_x`, `capitulation_drop_pct`, `near_200w_pct`*

### The buy-early rule
Your ladder already buys when the price falls to each rung. Bottom signals add one thing: **buy the next rung early**, before the price gets there, because several independent signs say the coin is washed out.

| Bottom signals on | Level | Your rule |
|---|---|---|
| 0–2 | — | Wait. Ladder orders stay in place; core buys continue. |
| **3** | Level 1 | Buy your **next ladder rung now** |
| **4–5** | Level 2 | Buy up to your **next 2 rungs** in total |
| **6** | Level 3 | Buy up to your **next 3 rungs** in total (the last rung stays as a reserve) |

- The dollar amount is exactly what those rungs would have spent, so it's never more than that coin's share of your dip reserve.
- Rungs bought early are marked done, so a limit order for them should be cancelled on your exchange. The *Dip reserve* tab only lists rungs you still need.
- Each level fires once per ladder. A new ladder anchor resets it.
- If the price reaches a rung on its own, that buy comes first.

## 6. The dip ladder

The ladder is your plan for buying drops with your **dip reserve** (cash set aside only for dips).

**Anchor:** the price the drops are measured from. By default it's the price on the **first run after a coin was added**, and the watcher remembers it. To measure from a different price, such as your average cost, set `"anchor"` for that coin in `coins.json`. Changing it resets that coin's ladder.

**Rungs (defaults):**

| Rung | Price | Spends |
|---|---|---|
| 1 | anchor − 10% | 25% of that coin's reserve |
| 2 | anchor − 20% | 25% |
| 3 | anchor − 30% | 25% |
| 4 | anchor − 40% | 25% |

**Dollar amount per rung** = *dip reserve pool* × *that coin's share %* × *rung share %*.
Example: $2,000 pool × SOL 30% × 25% = **$150** at each SOL rung.

**When a rung counts as "reached":**
- **Watcher:** if the **lowest price since the anchor was set** touched the rung. Once reached, it stays reached, just like a limit order that filled on a wick.
- **App:** also marks it reached right away if the **live price** is at or below it.

**On the card:** grey = waiting, blue = reached (go buy it, or check that your limit order filled), green ✓ = you logged the buy.

**Best practice:** put these as **limit orders on your exchange ahead of time**. The *Dip reserve* tab lists them. Then dips get bought even if you're asleep or frozen.

---

## 7. Buckets, trims and the trailing stop

Each coin is split into two buckets, set in **Setup**:

| Bucket | What it is | Rules that apply |
|---|---|---|
| **Core / staked** | Your long-term stack, e.g. what you stake | **Never sold on flags.** Only grows. |
| **Trade** | The part you're willing to trade around pumps | Flag trims and the trailing stop apply here **only** |

### Trims
When the flag count reaches a new level (section 4), the mission says exactly how many coins to sell, as a share of the trade bucket at the **start of the trim cycle**. The percentages are cumulative, so they don't compound:
- Level 1 → sell 25% of the starting trade bucket
- Level 2 → sell another 25% (50% total)
- Level 3 → sell another 25% (75% total), keeping 25% as the moonbag

When you log a trim, the **proceeds go into your dip reserve automatically**, so pumps fund your next dips.

**A trim cycle resets when:** you log a ladder-rung buy, you log a trailing-stop sale, or you press **Setup → Start a new trim cycle**.

### Trailing stop
Your stop **trails the highest price since your first trim of the cycle**, minus 25%. It starts at the price you logged for that first trim, and every new high after that (from live prices and the watcher's hourly and daily candles) raises it. It's drawn on the chart as **"your stop"** once you've trimmed.

**Mission fires when:** you've **trimmed at least once this cycle**, still hold trade coins, **and** the price is at or below the stop. The rule is to sell the moonbag. Core stays.

**Why from your trim, not an older high:** if the coin was much higher months ago, a stop measured from that old high could fire the moment you trim. Measuring from your own trim means the stop only protects the gains from *this* run-up.
*Setting: `trailing_stop_pct`*

---

## 8. How a coin's mission is chosen

The app checks these **in order** and shows the first one that applies:

| # | Mission | When |
|---|---|---|
| 1 | ⏸ **Rules paused** | The watcher hasn't published for **6+ hours**, so flags could be wrong |
| 2 | ⏸ **Data is stale** | This coin failed its last watcher run, so its old data is shown |
| 3 | 🛑 **Trailing stop hit** | Section 7 |
| 4 | 🔥 **Trim** | 3+ of the 6 flags and a higher level than you've already trimmed. With no trade bucket, it becomes a "don't chase it" warning instead. |
| 5 | 🪜 **Ladder rung reached** | A reached rung you haven't logged yet |
| 6 | 🧊 **Buy early** | 3+ of the 6 bottom signals and a higher level than you've already bought at (section 5) |
| 7 | 👀 **Hold · N/6 flags** | 1–2 flags: watch, don't act |
| 8 | ✅ **Hold** | Nothing triggered. Doing nothing *is* the move. |

Every mission says **why** (which flags or signals are on), and uses the **live price** whenever it's trustworthy (section 10).

---

## 9. Telegram alerts

The watcher sends a message **only when something changes**, never the same alert twice:

| Alert | When |
|---|---|
| ✅ Watcher is live | The very first run |
| 🔥 Top flags | The flag count reaches a higher level: 3, then 4, then 6 |
| 🪜 Ladder rung | A rung is reached for the first time |
| 🧊 Bottom signals | The bottom-signal count reaches a higher level: 3, then 4, then 6 |

There's no Telegram alert for the trailing stop, because it depends on when *you* trimmed, which only your app knows. The app's mission shows it instantly.

Telegram is optional. Without the two secrets set up, the watcher still runs; it just doesn't message you.

### The News tab
- **Hype meter:** one panel per coin showing daily mentions for the last 14 days (amber bars = the last 3 days), the spike ratio versus normal (or "baseline n/7 days" while it's still learning), catalyst and hype counts, trending rank, and whether flags 5–6 are on.
- **Sources line:** a ✓ or ✗ for each news feed this run, plus CoinGecko's trending list. A failed feed is skipped and shown, never hidden.
- **Headlines:** the last 7 days, filterable by coin (or **Market** for macro news) and by tag. Every headline links to the original article.

**The fixed sources** (in `coins.json` → `news_sources`):

| Source | What it covers |
|---|---|
| CoinDesk, Cointelegraph, Decrypt, The Block, CryptoSlate | General crypto news. A headline counts for a coin when it matches that coin's keywords. |
| Ethereum Foundation blog | Official, counts for ETH and is tagged **official** |
| Solana news (solana.com) | Official, counts for SOL and is tagged **official** |

No official feed was available for Ripple, so XRP is covered by the five outlets.

**How headlines are matched and tagged** (fixed word lists, no AI):
- **Coin match:** the coin's `keywords` as whole words, in the headline or its summary. ALL-CAPS tickers must match exactly, so "SOL" counts but "sol" or "solo" don't; names like "Solana" match in any case.
- **Tags** (headline only): **catalyst** (partnership, ETF, listing, approval, launch, mainnet, upgrade, integration, adoption…), **hype** (soars, skyrockets, surge, rally, all-time high, price prediction, could hit, 100x…), **risk** (hack, exploit, outage, lawsuit, delist, crash, probe…), **macro** (Fed, FOMC, rate cut/hike, inflation, CPI, tariffs, Powell…), **official** (from a project's own feed).
- The same story covered by 3 outlets counts as 3 mentions. More coverage means more hype, and that's what the meter measures.
- A tag can occasionally be wrong. Every flag shows the counts behind it, and the headlines are right there to check.

## 10. Trust guards: how it avoids false info

**No AI calculates any number or sets any flag.** Everything is plain, tested arithmetic on Kraken's data, fixed keyword rules on fixed news outlets, and CoinGecko's trending list.

**On GitHub (watcher):**
- **Maths self-test before every run** (30 checks: reference values, independent cross-checks, boundary scenarios). If it fails, nothing is published.
- **News safety:** headlines are de-duplicated across runs, never counted twice, and articles dated in the future are treated as "now". Only web links (`http`/`https`) are kept from feeds. If every news feed fails, flag 5 stays off for that run rather than guessing.
- **Bad data is rejected,** never used. A coin is rejected if Kraken returns fewer than 30 candles, candles out of time order, an impossible candle (low above the open or close, or high below them), or a latest candle that's too old.
- **If one coin fails,** the others still update, and that coin keeps its last good data, marked **stale**. Its mission pauses.
- **If the watcher can't read its own memory** (the previous `signals.json`) for any reason other than "it doesn't exist yet," it **aborts the run** rather than resetting your ladders.

**In the app:**
- **The live dot is honest.** It's green only while data is actually arriving. If the feed goes quiet for 60 seconds, the app reconnects on its own, waiting a little longer between attempts each time.
- **A live price older than 2 minutes** isn't used.
- **The mismatch guard:** if a live price is more than **25% away** from the watcher's price (and the watcher's data is fresh), the app **ignores the live price** for your rules and shows a warning. That catches a wrong coin name or a bad tick.
- **Freshness warnings:** a banner appears if signals are **over 75 minutes old** (normally they're under 20). After **6 hours**, all missions pause.
- **Every card shows its source:** "● live" or "watcher", and the footer shows how old the signals are.

---

## 11. The game: XP and levels

XP rewards **following your rules**, not making money. Profit is the market's job; discipline is yours.

| Action | XP |
|---|---|
| Daily check-in (once a day) | +10 |
| Logging a core buy | +15 |
| Buying a ladder rung | +50 |
| Trimming on flags | +60 |
| Following the trailing stop | +60 |
| Finishing a 24h cooldown instead of panic-selling | **+100** |
| Honestly logging a skip | +5 |
| Off-plan sell | −30 (and your streak resets) |

**Levels:** Paper Hands (0) → Rookie Stacker (150) → Ladder Climber (400) → Rule Follower (800) → Steel Core (1,400) → Cycle Rider (2,200) → Diamond Discipline (3,200).


---

## 12. What's public, what's private

| Public (anyone with the link) | Private |
|---|---|
| The code: app, watcher, tests | Holdings, average costs, buckets |
| `coins.json`: which coins, your rule settings, news sources | Dip reserve amounts, XP, journal, trims, your flag overrides: **your browser only** |
| `signals.json` + charts: market data | Telegram token and chat ID: **GitHub Secrets** |
| The app page (opens **empty** for anyone else) | Your GitHub login, your exchange accounts (never connected) |

**One rule:** never put amounts, balances or keys into any file in this repo.

**Backups:** progress lives in the browser you use. **Setup → Copy backup** now and then, and use **Restore** to move it to another device.

---

## 13. Changing things

Edit on GitHub (click the file, then ✏️, then **Commit changes**). Saving re-runs the watcher right away.

| To… | Edit | How |
|---|---|---|
| **Add a coin** | `coins.json` → `coins` | Add a line. The app's **＋ Add coin** writes it for you. Comma after every coin **except the last**. Include `"coingecko"` (the id in the coin's CoinGecko page address, e.g. `quant-network`) for the trending check, and `"keywords"` for news matching. |
| Add or remove a news outlet | `news_sources` | Any RSS or Atom feed URL. Add `"coin": "SOL"` if it's a project's own official feed. |
| Change what counts as a news mention | that coin's `keywords` | ALL-CAPS = exact case (tickers); other words match any case |
| Use a coin with no Kraken futures | that coin's line | `"futures": ""` |
| Fix a coin that never shows "● live" | that coin's line | Add `"ws": "ABC/USD"` with Kraken's live symbol |
| **Reset a ladder** / measure from your cost | `"anchor"` | Set a price, e.g. `"anchor": 1850` |
| Change rung depths or sizes | `ladder` | `drop` = % below anchor, `share` = % of that coin's reserve |
| Make a flag more or less sensitive | `thresholds` | See each flag in sections 4–5 |
| Check more or less often | `.github/workflows/watch.yml` → `cron` | Default `*/15 * * * *` (every 15 minutes) |
| **Update the app** | `site/index.html` | Open the `site` folder → **Add file → Upload files**, drop in the new file, then **Ctrl+F5** the app after the run |

---

## 14. Setup (first time)

Already done for **BHarsh17/crypto-coach**. Kept here for reference:

1. github.com → **+ → New repository** → name `crypto-coach`, **Public**, nothing ticked → Create.
2. **uploading an existing file** → drag in `README.md`, `coins.json`, and the `site`, `tests` and `watcher` folders → Commit.
3. **Settings → Pages → Source: GitHub Actions.**
4. **Add file → Create new file** → name it `.github/workflows/watch.yml` → paste the workflow → Commit. (This folder is often skipped by drag-and-drop.)
5. *(Optional)* **Telegram:** message @BotFather → `/newbot` → copy the token. Send your bot any message. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `"chat":{"id": …}`. Then go to **Settings → Secrets and variables → Actions** and add `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
6. **Actions** → the run turns green → open the app → **Setup**.

---

## 15. Troubleshooting

| You see | It means | Do this |
|---|---|---|
| ❌ Red X in **Actions** | A run failed | Click it, then the red step, and copy the error to Claude |
| "Signals are N minutes old" | Runs are failing or delayed | Check **Actions** |
| Top-right dot grey or amber | Live feed down or reconnecting | Wait a few seconds; check your internet; reload |
| A card says "watcher", never "● live" | That coin's live symbol doesn't match | Add `"ws": "ABC/USD"` in `coins.json` |
| "Live price disagrees…" banner | Live and watcher prices are more than 25% apart | Check that coin's `kraken` / `ws` names |
| Leverage flag says "no futures data" | No Kraken futures market for that coin | Normal. That flag stays off. |
| News flag says "building baseline (n/7 days)" | The hype meter is still learning what's normal | Normal for the first week. It uses the 8-mentions rule meanwhile. |
| A ✗ next to a news source | That feed failed this run (site down or blocking GitHub) | Usually clears by itself. If it stays red for days, remove it or swap the URL in `news_sources`. |
| "trending list unavailable" | CoinGecko's free list didn't respond | The last good list is reused for up to 6 hours; the hype-headline part still works |
| Old version of the app after an update | Browser cache | **Ctrl+F5** |
| Email: "scheduled workflow disabled" | GitHub pauses schedules after **60 days with no repo changes** | **Actions → Enable workflow**, or edit any file |
| New coin doesn't appear | Usually a comma problem in `coins.json` | Comma after every coin except the last; check **Actions** for errors |

### Moving to the homelab later
`watcher/watch.py` is plain Python with no installs. On the mini PC, run
`python3 watcher/watch.py --prev site/signals.json` from cron or an n8n "Execute Command" node, and serve the `site` folder with any web server. It's the same app, with no GitHub needed and nothing public.
