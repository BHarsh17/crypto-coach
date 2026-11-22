# Crypto Coach strategy (v5 draft, under test)

**Owner:** B (Kelowna, BC, Canada). Retail investor with a full-time job, not a day trader.
**Goal:** accumulate more coins over time (ETH, SOL, XRP; QNT and BTC tracked). Success is measured in **coins**, not dollars.
**Main problem being solved:** B freezes during crashes, then sells right before rebounds. The system replaces gut calls with fixed rules and phone pings. B still places every trade himself on Kraken (spot only, no leverage, no exchange API keys).

---

## 1. Money and buckets (per coin, each coin runs independently)

- **New money:** a fixed monthly amount per coin (tested with $100/month), plus any cash already set aside.
- Every buy is split:
  - **Core: 40%**, never sold. This is the long-term stack, and it only grows.
  - **Trade: 60%**, which becomes a **lot** (see ladder 2).
- **Minimum order:** $5 (Kraken minimums). Anything smaller isn't placed.

## 2. The three ladders

### Ladder 1 · Buy-in ladder: new money in
- **Baseline:** the highest price in the last **30 days**.
  - Trails upward while nothing has been bought.
  - Freezes once a rung fills.
  - Resets when all 4 rungs have filled, or when the price closes back above the frozen baseline.
- **Rungs:** baseline −10%, −25%, −40%, −55%.
- **Size:** each rung uses an equal share of the cash waiting: 1 ÷ (rungs not yet filled).
- **Rungs already passed** when the ladder is armed buy at the market price right away.
- **Max wait:** monthly money that no rung has used within **60 days** is bought anyway at market. This stops a strong uptrend from leaving B in cash.
- Knows nothing about existing lots.

### Ladder 2 · Sell ladder: one per lot
- Each lot's own buy price (fees included) is its base.
- **+20%:** sell half the lot. **+40%:** sell the rest.
- If half a lot is worth under $5, the whole lot sells at +20%.

### Ladder 3 · Buy-back ladder: one per sale
- Based on that sale's price: **−20%** and **−40%**, each using half the sale's money. If the halves would be under $5, one order at −20% uses all of it.
- Each buy-back becomes a **new lot** with its own sell ladder.
- Once a sale's buy-backs have bought back **more coins than were sold**, the extra coins move to **Core**. This is how trading gains turn into kept coins.
- Sale money that is never re-bought (price never comes back down) stays as cash.

```
New money → Buy-in ladder → 40% Core + 60% new lot
Lot → Sell ladder → cash → Buy-back ladder → new lot (+ extra coins to Core)
```

## 3. Signals (information only, never trade by themselves)
- **6 top flags** (vertical move, stretched above averages, RSI ≥85 daily / ≥80 weekly, leverage/funding, news priced in, social FOMO). 3+ on = "HEADS UP · overheated".
- **6 bottom signals** (freefall, stretched below averages, RSI ≤25 daily / ≤30 weekly, shorts crowded, capitulation volume, near the 200-week average). 3+ on = "HEADS UP · washed out".
- Tested as a buy trigger (wait until washed out and the price closes back above its 20-day average). It did slightly **worse** than the plain buy-in ladder, so it's kept as context only.

## 4. Mechanics
- A GitHub Actions watcher runs every 15 minutes on Kraken public data (hourly candles since the last run) and pings B's phone (self-hostable ntfy) when any order price is reached.
- Orders are meant to sit on Kraken as limit orders, so most fill without B watching.
- The app tracks lots privately in the browser. The watcher only needs order **prices**, never amounts.
- An order created during a check can't fill until the next check (no same-candle look-ahead).
- No stop-losses. No leverage. No shorting.

## 5. Backtest evidence (Kraken daily candles, 0.25% fee per trade)

**Accumulate from zero**, 2025-01-31 → 2026-11-21 (660 days), $100/month per coin, $2,300 total per coin. Score = coins held + cash waiting converted at the final price, compared with monthly DCA. Stress test: also started 3, 6 and 9 months later (20 start/coin combinations), and each half of the period run on its own.

| Model | Coins beating DCA | Start dates beating DCA | Halves beating DCA | Average vs DCA | Worst coin | Worst of any start | Trades |
|---|---|---|---|---|---|---|---|
| **A lots (this strategy)** | 4/5 | 16/20 | 9/10 | +13.9% | −9.1% | −24.3% | 876 |
| A, one average cost for the bucket | 4/5 | 16/20 | 9/10 | +12.1% | −9.1% | −25.9% | 225 |
| B, washed out (3+) + turning up | 4/5 | 16/20 | 9/10 | +11.4% | −10.8% | −22.7% | 171 |
| B, washed out (2+) + turning up | 4/5 | 16/20 | 9/10 | +9.4% | −11.1% | −23.1% | 148 |

Per coin, A lots vs DCA: ETH +19.0%, SOL +22.5%, XRP +26.3%, BTC +10.6%, QNT −9.1%.
Price change over the period: ETH −37%, SOL −61%, XRP −62%, BTC −46%, QNT +46% (the only riser, and the only loser for every model).

**Earlier test** (start holding $1,000 of the coin + $500 reserve, 18 months), comparing sell spacings with buy-backs at the same distance: +10/+20% → 3/5 coins beat DCA (avg +2.7%); **+20/+40% → 5/5 (avg +10.8%, worst +2.6%)**; +30/+60% → 4/5 (avg +17.4%, worst −1.4%).

## 6. Known weaknesses and open questions
1. **Strong bull markets:** selling into a steady rise loses to just holding (QNT). Core (40%) is the only exposure that rides the whole move.
2. **Cash drag:** sale money waits for buy-backs that may never fill (QNT ended with ~$1,400 waiting).
3. **One window:** 22 months, mostly a falling market for 4 of 5 coins. Daily candles. Small sample.
4. **Parameters partly chosen, not optimized:** the 30-day baseline, the 40/60 split and the 60-day max wait were picked, not tested against alternatives. +20/+40 was chosen from 3 tested spacings.
5. **Fill realism:** the backtest fills limit buys at the rung price even when the day gapped below it (pessimistic). To be fixed: fill at min(rung, open).
6. **Workload:** lots need ~4× the orders of one-average tracking (876 vs 225 over 22 months across 5 coins).
7. **Tax (Canada):** every sell is a taxable disposition. The CRA uses average cost (ACB) across all identical units, so per-lot tracking is a trading device only, not a tax basis.
8. **Correlation:** all the coins tend to crash together, so the ladders on every coin fire at once.

## 7. Independent review (3 reviewers, 2026-11-22) and what changed

Three independent AI reviews took different roles (quant, risk manager, crypto swing trader). None of them saw the design reasoning, only this document.

**All three agreed:**
- The structure fixes the real problem: resting orders fill even when B freezes, and Core is never sold.
- The evidence was too thin: one 22-month, mostly falling window, and correlated coins.
- Bull markets are the weak spot: sale cash sits unused while prices run.
- Fees and Canadian tax were understated.

**Main critique (quant):** "coins + cash" flatters any strategy that holds cash in a falling market. The test needs a cash-matched benchmark and random-timing controls.

**Changes being tested** (backtest/stress.py, v4.3):
- Lots: each buy / one lot per month / one average.
- Core 40 / 50 / 70%.
- Bull mode: above a rising 200-day average, keep half of the +40% sale as a runner (sold at +100%, or when the price closes below the 50-day average).
- 120-day stale-cash rule: unused buy-back money goes back into buy-ins.
- Passed rungs: at most one market buy a week.
- B's real constraints: $500 start, $100/month, at most 3 buy-ins a month, $5 minimum order.
- Fees: 0.40% taker, 0.25% maker. Fills at the gap price. Tax estimate on CRA average cost.
- History back to 2016-17. Rolling 2-year windows, split by bull / bear / sideways. Bootstrap paths. Sensitivity of the baseline window (30 / 60 / 90 days) and of the spacing.

**Practical guardrails to adopt regardless of the results:**
- Decide a maximum total amount and a drop you can stomach, and write both down.
- Keep the emergency fund and goal savings out of crypto entirely.
- Never cancel orders during a drawdown.
- One daily ping, sent at a fixed time.
- Staked coins = Core (they can't fill sell orders).
- Keep tax records from day 1 (export the Kraken ledger every quarter).
- Check with a CPA whether trading this often counts as business income.
