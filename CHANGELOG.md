# Crypto Coach changelog

The version number lives in `VERSION`. The app shows it in its footer and warns if the app and the watcher versions don't match, which usually means one file wasn't uploaded.

Numbering: **major.minor.patch**. Major = a change to how the rules work. Minor = a new feature. Patch = a fix or a tidy-up.

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
