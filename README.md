# Crypto Coach

Your rules, not your gut. A free watcher on GitHub scores your coins every 15 minutes and pings your phone when a rule triggers. A game-style app shows you what your rules say to do and gives you XP for following them.

```
GitHub Actions (every 15 min, free) ── the heavy lifting
  ├─ tests/test_watch.py  checks the maths first; if it fails, nothing is published
  └─ watcher/watch.py ── Kraken public data (no account, no keys)
        ├─ refuses bad data (missing, out-of-order or old candles)
        ├─ scores top flags 1–4, bottom signals, dip ladder
        ├─ Telegram alert only when something CHANGES
        └─ publishes signals.json + chart candles ─► GitHub Pages ─► the app

The app (your phone/PC)
  ├─ reads the watcher's signals (rules, flags, ladder)
  └─ streams live prices + candles straight from Kraken's public WebSocket while it's open
```

**Trust guards built in:** the app shows a green **live** dot only while Kraken's feed is actually flowing. It ignores a live price that disagrees with the watcher by more than 25% (wrong coin name, bad tick). It warns if signals are over 75 minutes old, and it pauses all missions if the watcher hasn't run for 6 hours. No AI is involved in any number. It's plain, tested arithmetic on Kraken's data.

Your holdings, dip reserve, XP and journal stay **in your browser only**. The repo holds only market data and your rules.

---

## Setup (about 20 minutes, once)

### 1. Create the repo
1. Sign in at github.com → **New repository** → name it `crypto-coach` → **Public** → Create.
   *Public is what makes Pages and Actions free with no minute limits. Nothing personal goes in it.*
2. Click **uploading an existing file**. Drag in `README.md`, `coins.json`, and the `watcher`, `tests` and `site` folders. Commit.
3. The `.github` folder is hidden on Mac/Windows and often doesn't upload, so create it by hand: **Add file → Create new file**, type the name `.github/workflows/watch.yml`, paste the contents of that file from this package, and commit.

### 2. Turn on Pages
**Settings → Pages → Build and deployment → Source: GitHub Actions.**

### 3. Telegram alerts (optional but recommended)
1. In Telegram, message **@BotFather** → `/newbot` → pick a name → copy the **token**.
2. Send any message to your new bot.
3. Open `https://api.telegram.org/bot<TOKEN>/getUpdates` in a browser → find `"chat":{"id": 123456789` → that number is your **chat id**.
4. In your repo: **Settings → Secrets and variables → Actions → New repository secret**:
   - `TELEGRAM_BOT_TOKEN` = the token
   - `TELEGRAM_CHAT_ID` = the chat id

### 4. First run
**Actions tab** → enable workflows if asked → **Crypto Coach watcher** → **Run workflow**. In about a minute you get a "✅ watcher is live" message.

### 5. Open the app
`https://<your-github-username>.github.io/crypto-coach/`

Go to **Setup** and enter, per coin: core/staked amount, trade amount, rough average cost, and the share of your dip reserve. Then set the reserve pool total. On your phone, use **Add to Home Screen** so it opens like an app.

---

## Daily use
- **Charts:** tap a sparkline or 📈 Chart. Live candles with your 20/50/200 averages, ladder rungs, trailing stop and avg cost drawn on. Decide on **1D**, zoom out on **1W**. 1H/4H are for watching, not acting.
- **Telegram pings** only when a rule triggers: a ladder rung is reached, top flags reach 3+, 2+ bottom signals show up, or a trailing stop is hit.
- **Open the app**, read each coin's **mission**, do it (or don't), and log it. XP is for following your rules, not for profit.
- **Flags 5–7** (old wallets moving, news priced in, social FOMO) are judgment calls: tap them when you see them. They expire after 7 days.
- **😰 I want to sell** shows your rules and starts a 24-hour cooldown. Finishing it earns the biggest XP in the game.
- **Backup**: Setup → Copy backup now and then. Progress lives in that browser.

## Changing things
| To… | Edit | Notes |
|---|---|---|
| Add a coin | `coins.json` → add a line to `coins` | The app's **＋ Add coin** button writes the line for you. Kraken pair names: `QNTUSD`, `ADAUSD`… Set `futures` to `""` if there's no Kraken perpetual. |
| Reset a coin's ladder | set `anchor` to a price | `null` = the price on the first run. Changing it resets the rungs. |
| Change rung sizes | `ladder` | `drop` = % below anchor, `share` = % of that coin's reserve. |
| Tune the flags | `thresholds` | E.g. `rsi_daily_hot: 85`, `trailing_stop_pct: 25`. |
| Check more or less often | `.github/workflows/watch.yml` → `cron` | Default `*/15 * * * *`. Faster adds nothing: the flags use daily and weekly candles, and the app already shows live prices. |
| Live symbol differs from `SYMBOL/USD` | add `"ws": "XYZ/USD"` to the coin | Only needed if the live dot never turns on for that coin. |

Saving any of these on GitHub re-runs the watcher right away.

## Good to know
- **GitHub pauses scheduled runs after 60 days without any change to the repo.** It emails you first. Click "Enable workflow", or edit any file.
- Scheduled runs can start a few minutes late when GitHub is busy. That's fine for this.
- If Kraken has no futures market for a coin, the leverage flag shows "no futures data" and simply stays off.
- These are rule-based signals, not predictions. They make sure *your* plan gets followed. They can't promise the market will cooperate.

## Moving to the homelab later
`watcher/watch.py` is plain Python with no dependencies. On the mini PC: a cron job or n8n "Execute Command" node runs
`python3 watcher/watch.py --prev site/signals.json`, and any web server (or n8n) serves the `site` folder. Same app, no GitHub needed.
