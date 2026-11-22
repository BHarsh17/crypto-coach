# Crypto Coach changelog

The version number lives in `VERSION`. The app shows it in its footer and warns if the app and the watcher versions don't match, which usually means one file wasn't uploaded.

Numbering: **major.minor.patch**. Major = a change to how the rules work. Minor = a new feature. Patch = a fix or a tidy-up.

## 4.0.1 · 2026-11-22
- **Fix (found by checking the real backtest):** when a second buy-back lowered your average, the re-priced take-profit could "fill" on the same candle, even though that order didn't exist yet (SOL, 10 Oct 2025). Re-priced take-profits now wait for the next check, live and in the backtest. New test added.

## 4.0.0 · 2026-11-22
**New rules, based on the 18-month backtest.** Your own method, tuned to the spacing that beat monthly DCA on all 5 coins:
- **🔴 JUMP OUT:** take-profits at **+20%** and **+40%** above your base price (your average cost), each selling half your trade bucket.
- **🟢 JUMP IN:** after each sale, buy-backs at **20%** and **40%** below the sale price, each spending half of that sale's money. A buy-back starts a new cycle, measured from what you paid.
- **🟢 Dip ladder:** your dip reserve at −10 / −25 / −40 / −55%.
- **🟡 HEADS UP:** the 6 top flags and 6 bottom signals become warnings ("overheated: check your sell orders", "washed out: check your buy orders") instead of triggers.
- Removed: flag-based trims, the trailing stop, and buy-early (none of them held up in testing).

**One engine, live and tested:** `watcher/engine.py` runs the orders for the live alerts *and* for the backtest's new "v4" row, so what's tested is what runs. It checks every hourly candle since the last run, so spikes between runs aren't missed.

**Alerts:** ntfy (recommended; self-hostable), Telegram, and WhatsApp via CallMeBot, in any combination. JUMP alerts are high priority.

**App:** each card shows your orders with prices and amounts, and the mission says exactly what to do ("Sell 3 SOL", "Buy back with $180"). The Dip reserve tab lists every order to have on Kraken. Setup shows the take-profit base the watcher uses next to your real average cost. Live prices can trigger JUMP IN / JUMP OUT on the card before the watcher's next run.

**Upgrade note:** `coins.json` gains `"cost"` per coin and a `rules` block. The workflow file needs the new alert secrets (see README).

## 3.3.0 · 2026-11-22
- **Backtest now checks your own trading method** at three spacings: take-profits at +10/+20%, +20/+40% and +30/+60% above your average cost (half the trade bucket each), with buy-backs the same distance and double the distance below each sale.
- **Rise ladder strategies:** take-profits at +25/50/75/100% (a quarter each), with and without "sell the next one early when overheated".
- **Overall ranking** at the top of the Backtest tab: for each approach, how many coins it beat monthly DCA on, its average, and its worst case.
- Backtest starts with 40% core / 60% trade, like keeping 4 of 10 coins.
- More realistic fills: an order placed today can't fill until tomorrow, because a daily candle doesn't show whether its high or low came first.

## 3.2.0 · 2026-11-22
- **Backtest scoring is fairer:** the score is now total value in coins (coins + cash converted at the final price), so selling high and holding cash, like QNT's trims, counts properly.
- **Two new strategies to test:** "Ladder moves after trims" (after each trim, the ladder restarts from the trim price using the cash on hand) and "Half monthly + half deep ladder" (half the reserve bought monthly, half on a −10/−25/−40/−55% ladder that also moves after trims; no buy-early). Replaces the earlier "proposed tweaks".
- Small amounts like BTC now show 4 significant digits instead of being rounded to 0.015.
- These are backtest-only changes; the live rules are unchanged until the results are in.

## 3.1.0 · 2026-11-22
- **Backtest.** The watcher now replays the rules on the last 18 months of real Kraken prices for every coin in `coins.json`, plus BTC, once a day. New **🧪 Backtest** tab compares: the app as built, ladder only, proposed tweaks, monthly DCA, all-in on day 1, and keeping the cash. It never blocks the live signals; if it fails, the rest of the run carries on.

## 3.0.0 · 2026-11-22
- **Buy side rebuilt to mirror the sell side.** Bottom signals go from 3 to **6**, each the mirror image of a top flag: Freefall, Stretched below, Oversold RSI (daily ≤ 25 or weekly ≤ 30), Shorts crowded (negative funding), Capitulation volume, Near 200-week average.
- **New rule: buy early.** At 3 bottom signals, buy your next ladder rung now instead of waiting for the price to reach it; at 4–5, the next 2 rungs in total; at 6, the next 3. It never spends more than that coin's ladder, and a new ladder anchor resets it.
- Telegram alerts for bottom signals now fire at 3, 4 and 6, just like the top flags.
- Removed the old "accumulation zone at 2 of 3" message (replaced by the rule above).

## 2.2.0 · 2026-11-22
- **Removed badges.** XP, levels and streaks stay. "How XP works" moved to the top of the Journal tab.
- **Added version numbers** (`VERSION`, this changelog, the app footer, `signals.json`), plus a warning banner when the app and watcher versions differ.
- **Automatic deploy retry:** if GitHub can't publish on the first try, the run waits 45 seconds and tries once more.
- **Calmer cards:** fixed-size mission boxes, cards that line up, fixed-width digits, no flashing, and redraws only when a mission changes.
- Self-test now also checks that `VERSION`, the app and this changelog agree.

## 2.0.0 · 2026-11-21
- **News engine:** reads 7 fixed news feeds and CoinGecko's trending list each run. New **News** tab with a hype meter and matched headlines.
- **Flags are now all automatic, 6 in total.** Dropped "old money moving" (no reliable free source). "News priced in" and "Social FOMO" are set by fixed rules, with a 7-day tap-to-override.
- **Trailing stop reworked:** it now trails the peak since *your first trim*, not the 90-day high, so it can't fire right after you trim. Removed the watcher's stop alert, since only the app knows when you trimmed.
- **Maths fixes:** a flat price read as RSI 100 (now 50); open-interest growth could compare against a 1-day-old reading (now needs 6+ days); funding is converted to a per-8-hour rate from the actual period length; average cost no longer goes wrong when buying with no cost set; ladder memory is safer with incomplete earlier data.
- Self-test grew to 30 checks: reference values, independent cross-checks, boundary scenarios and back-to-back runs.

## 1.2.0 · 2026-11-21
- Card "● live" badges turn green when live; card sparklines follow the live price.

## 1.1.0 · 2026-11-21
- Live prices and live candle charts (1H/4H/1D/1W) from Kraken's public WebSocket.
- Watcher every 15 minutes; data validation; maths self-test before publishing; stale-data and price-mismatch guards.

## 1.0.0 · 2026-11-21
- First version: watcher on GitHub Actions, 4 measured top flags, 3 bottom signals, dip ladder, buckets, missions, Telegram alerts, XP and journal.
