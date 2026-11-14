# GoldBot — WARDOGS gold-bar price alert for Telegram

Watches the WARDOGS gold exchange rate and sends you a Telegram message whenever one
gold bar costs **less than $400,000** (configurable).

The in-game rate is fixed for a whole UTC day and changes at midnight UTC, so GoldBot
sends at most one alert per day, not one per poll.

---

## What I found on wardogshub.gg/gold-market

Investigated with REA (passive browser capture + static JS analysis):

| Question | Finding |
|---|---|
| Is the price in the HTML? | Yes. The whole daily series is server-rendered into `<script type="application/json" data-gold-series>` — 584 points, `{date, rate}`. |
| Does the browser fetch gold data? | No. Runtime capture showed only page assets plus `/api/session`. No Supabase/XHR for gold. |
| Is there a public gold API on wardogshub.gg? | No. `/api/gold`, `/api/gold-rate`, `/api/market/gold`, `/gold-market.json` all return 404. |
| Where does the site get it? | Its own chart JS says: *"On the live site this file is the fallback; the page reads the daily pull from Supabase"*, refreshed from **MetaForge's public feed** by `scripts/pull-gold-rate.mjs`. |
| Is the page fresh? | No. On 14 Nov 2026 it still ended at **2026-11-05 ($641,920)** — 9 days stale. |
| What is fresh? | MetaForge's feed: `https://metaforge.app/api/wardogs/market` — updated the same hour, latest **2026-11-14 ($896,944)**. |

So GoldBot uses **MetaForge's JSON feed first** and falls back to parsing the
wardogshub.gg page if MetaForge is unreachable.

### Data contract used

```http
GET https://metaforge.app/api/wardogs/market
```
```json
{
  "exchangeId": "Attribute.Meta.Currency.GoldBars",
  "updatedAt": 1794687955703,
  "points": [{ "t": 1756944000000, "price": 352600 }, "..."],
  "stats": { "current": 896944, "min": 166000, "max": 2798240, "changes": { "7d": 1.09 } }
}
```
`t` and `updatedAt` are Unix milliseconds; one point per UTC day.

Fallback: `GET https://wardogshub.gg/gold-market/` then read
`data-gold-series` → `[{"date":"YYYY-MM-DD","rate":123456}, ...]`.

---

## Setup

1. **Create the bot.** In Telegram, message [@BotFather](https://t.me/BotFather) →
   `/newbot` → copy the token.
2. **Get your chat id.** Open Telegram, find your bot, press **START** (or send it
   `hi`), then run:

```powershell
python goldbot.py --chat-id
```

It prints your numeric id. (Equivalent manual route: open
`https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates` and read
`result[0].message.chat.id`.) An empty `result` simply means the bot has not
received a message yet.
3. **Configure.** Copy `.env.example` to `.env` and fill in:

```ini
TELEGRAM_BOT_TOKEN=123456789:AA...
TELEGRAM_CHAT_ID=123456789
GOLD_THRESHOLD=400000
```

4. **Test the wiring.**

```powershell
python goldbot.py --test
```

5. **Run it.**

```powershell
python goldbot.py                 # loop forever, checks every 15 min
python goldbot.py --once          # single check (good for Task Scheduler)
python goldbot.py --once --dry-run --threshold 1000000   # preview an alert, send nothing
```

On Windows you can also double-click `run_goldbot.bat`.

---

## Telegram commands

Message your bot any of these:

| Command | Reply |
|---|---|
| `/price` (or `/gold`) | Current price, day, move vs yesterday, and whether it is under your target |
| `/history` or `/history 14` | Last N daily rates (default 7, max 30) |
| `/status` | Threshold, source, poll interval, last alert, last check |
| `/help` | Command list |

Quick check from the terminal without Telegram:

```powershell
python goldbot.py --price
```

## Behaviour

- Alerts only when `price < GOLD_THRESHOLD`.
- One alert per distinct `(date, price)` — if the price stays under 400k for three
  days you get three alerts; polling the same day again sends nothing.
- Includes price, day, move vs previous day, threshold, source and the market link.
- If the source is more than one day behind, the alert says so.
- Transient network errors are logged and retried on the next poll.
- State is stored in `goldbot_state.json` (delete it to allow a re-alert for the
  current day).

## Scheduling (optional)

Run once every 15 minutes with Windows Task Scheduler:

```powershell
schtasks /create /tn "GoldBot" /sc minute /mo 15 ^
  /tr "python C:\Users\PsYcHo\Desktop\goldBot\goldbot.py --once"
```

Or simply leave `python goldbot.py` running in a terminal. On Linux/macOS use cron:

```cron
*/15 * * * * cd /path/to/goldBot && python3 goldbot.py --once
```

## Run it on GitHub Actions (no PC needed)

The workflow at `.github/workflows/goldbot.yml` runs `goldbot.py --once --state-minimal`
on a schedule, so your computer does not have to stay on.

1. Create a GitHub repository and push this folder:

```powershell
git init
git add .
git commit -m "GoldBot"
git branch -M main
git remote add origin https://github.com/<your-name>/<repo>.git
git push -u origin main
```

2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**

   | Secret | Value |
   |---|---|
   | `TELEGRAM_BOT_TOKEN` | your BotFather token |
   | `TELEGRAM_CHAT_ID` | your numeric chat id |

   Optional: add a **variable** `GOLD_THRESHOLD` (default `400000`).

3. Open the **Actions** tab → **GoldBot** → **Run workflow** to test once.

What it does on each scheduled run: checks the price, answers any pending
`/price`, `/history`, `/status` or `/help`, and commits `goldbot_state.json`
**only** when an alert was sent or a command was answered — that file prevents
duplicate alerts between runs. It holds no secrets.

### GitHub caveats

- The schedule is every 15 minutes (`*/15 * * * *`).
- GitHub cron is best-effort: runs can start a few minutes late, and occasionally
  later under load. The game rate is daily, so this is fine for alerts.
- Telegram **commands are answered only during a scheduled run** (usually within
  15 minutes, longer if GitHub delays the run). If you want instant `/price`
  replies, keep the local server running or use an always-on host.
- **Public repo** = free unlimited Actions (recommended for 15-minute runs), but
  GitHub disables scheduled workflows after 60 days with no repository activity.
  **Private repo** = 15-minute runs cost roughly 2,880 minutes/month, which
  exceeds the free 2,000; switch to hourly (`5 * * * *`) there.
- Never commit `.env` — it is already in `.gitignore`. Secrets belong in
  GitHub Secrets.

No third-party packages are required — Python 3.8+ standard library only.
