# ESPN Fantasy Football → Discord notifier

Posts your ESPN fantasy league's transactions, weekly results, and lineup lock reminders to a Discord
channel. Runs entirely on GitHub Actions cron — no server, no always-on process, no Discord bot.

It is a one-shot Python script polled every 5 minutes. All persistence lives in `state.json`, which the
workflow commits back to the repository.

## What it posts

Transactions, grouped so a trade reads as one message rather than six:

```
🔁 Trade processed
  Sofa Sharks gets: Amari Cooper, Rhamondre Stevenson
  Waiver Wolves gets: Jaylen Waddle
```

```
📥 Waiver Wolves
  + Marvin Waivers Jr. ($42 waiver)
  − Cordarrelle Patterson
```

Weekly results once a week is complete, with the week's high and low:

```
🏈 Week 3 Results
  ✅ Waiver Wolves 134.8 — Sofa Sharks 118.6
  🤝 Team C 101.0 — Team D 101.0 (tie)
  💤 Backup Badgers 88.0 (bye)

  📈 High: Waiver Wolves — 134.8
  📉 Low: Sofa Sharks — 118.6
```

Lineup lock reminders, 30 minutes before Thursday and Sunday kickoff:

```
⏰ Lineups lock in 30 minutes for the Sunday early games.
```

## Make this repository public

**Recommended: make this repository public.**

Private repositories get 2,000 free Actions minutes per month. A run every 5 minutes is roughly 8,600
runs a month, so a private repo would exhaust its free minutes within days and the notifier would stop.
Public repositories get unlimited Actions minutes.

Secrets are encrypted either way — a public repo does **not** expose your cookies or webhook URL. What
does become publicly visible is `state.json`, so it is worth knowing exactly what that file contains:

| Key | Contents |
|---|---|
| `last_activity_ms` | One integer: the epoch-millisecond timestamp of the newest transaction seen |
| `seen_fingerprints` | Truncated SHA-256 hashes. Not reversible into names |
| `posted_weeks` | Week numbers already announced, e.g. `[1, 2, 3]` |
| `posted_reminders` | Date-and-slot keys, e.g. `"2026-11-09-sunday"` |
| `error_notices` | Epoch-millisecond timestamps used to rate-limit error reports |

No player names, team names, scores, league id, or anything derived from a cookie is ever written to
`state.json`. The file reveals roughly *when* your league is active, and nothing about what happened.

If you would rather keep the repo private, expect to either pay for Actions minutes or widen the cron
interval considerably.

## Requirements

- A GitHub account
- An ESPN fantasy football league you can log into
- A Discord server where you can create webhooks
- Nothing installed locally, unless you want to run the tests

## Setup

### 1. Get this repository

Fork it, or push a copy to a new **public** repository of your own.

### 2. Get your ESPN cookies

Private leagues require two cookies from a logged-in browser session.

1. Log in at [fantasy.espn.com](https://fantasy.espn.com) and open your league.
2. Open developer tools (`F12`).
3. Go to **Application → Storage → Cookies → `https://fantasy.espn.com`** in Chrome or Edge, or
   **Storage → Cookies** in Firefox.
4. Find these two entries and copy their full values:
   - **`espn_s2`** — a long URL-encoded string. Copy all of it; it is easy to truncate by accident.
   - **`SWID`** — looks like `{XXXXXXXX-XXXX-XXXX-XXXX-XXXXXXXXXXXX}`. The braces are optional; the
     script adds them if they are missing.

You also need your **league id**, the `leagueId=` number in your league's URL.

> **These cookies are equivalent to being logged into your ESPN account.** Treat them like a password.
> Never paste them into a file in this repository, an issue, or a commit message.

### 3. Create a Discord webhook

1. In Discord, open **Server Settings → Integrations → Webhooks**.
2. Click **New Webhook**, choose the channel you want notifications posted to, and give it a name.
3. Click **Copy Webhook URL**.

That one webhook is enough — everything posts to that single channel. To split messages across several
channels, see [Routing to multiple channels](#routing-to-multiple-channels) below.

> **A webhook URL is a credential.** Anyone who has it can post to that channel as your webhook. Keep it
> in repository secrets only.

### Routing to multiple channels

A Discord webhook is permanently bound to **one** channel, so each destination needs its own webhook.
Create one per channel exactly as above, then add whichever of these you want:

| Secret | What lands there |
|---|---|
| `DISCORD_WEBHOOK_URL` | Everything not listed below: lineup reminders, the startup message, and any error notices |
| `DISCORD_WEBHOOK_URL_TRADES` | Trades |
| `DISCORD_WEBHOOK_URL_ROSTER` | Waiver claims, free agent adds, and drops |
| `DISCORD_WEBHOOK_URL_RESULTS` | Weekly results |

**All of these are optional except the first.** Anything left unset falls back to
`DISCORD_WEBHOOK_URL`, so you can split only the parts you care about — routing trades to their own
channel and leaving everything else together is a perfectly reasonable setup.

Errors and the startup confirmation always go to `DISCORD_WEBHOOK_URL`. That is deliberate: a "cookies
have expired" warning delivered to a channel nobody watches is barely better than no warning at all.

Run `python poller.py --dry-run` locally to see which channel each message type would reach.

### 4. Add repository secrets

Go to **Settings → Secrets and variables → Actions → Secrets → New repository secret** and add:

| Secret | Required | Value |
|---|---|---|
| `LEAGUE_ID` | yes | Your league id, e.g. `1234567` |
| `LEAGUE_YEAR` | yes | The season year, e.g. `2026` |
| `ESPN_S2` | yes | The `espn_s2` cookie value |
| `SWID` | yes | The `SWID` cookie value |
| `DISCORD_WEBHOOK_URL` | yes | Main webhook; also the fallback for anything below |
| `DISCORD_WEBHOOK_URL_TRADES` | no | Trades only; falls back to the main one |
| `DISCORD_WEBHOOK_URL_ROSTER` | no | Waivers, adds, and drops; falls back to the main one |
| `DISCORD_WEBHOOK_URL_RESULTS` | no | Weekly results; falls back to the main one |

### 5. Add repository variables (optional)

These two are not sensitive, so they live under the **Variables** tab rather than Secrets, where they
are easier to read and edit.

| Variable | Default | Value |
|---|---|---|
| `TIMEZONE` | `America/Chicago` | Any IANA zone name, e.g. `America/New_York` |
| `LINEUP_REMINDERS` | `true` | `false` to disable lineup lock reminders |

Both can be left unset.

### 6. Send a test message (optional, recommended)

Before the first real run, check that your webhooks are right and that messages land in the channels you
expect. Go to **Actions → Fantasy notifier → Run workflow**, set **mode** to **demo**, and run it.

That posts one of each message type — a trade, a waiver claim with a drop, a free agent add, weekly
results, and both lineup reminders — to whichever channels you configured. The batch is bookended with
`🧪 Notifier test starting` and `🧪 Test complete` so nobody in the server mistakes a sample trade for a
real one. Delete the samples afterwards.

Demo mode writes no state and never contacts ESPN, so it cannot consume your first run or skip anything.
Scheduled runs receive no inputs and always take the normal path — cron can never post sample data.

### 7. Do the first real run

Go to **Actions → Fantasy notifier → Run workflow**, leaving **mode** on **normal**.

On the very first run you should see:

- Exactly **one** message in Discord: `✅ Fantasy notifier is online.`
- A new commit to `state.json` from `github-actions[bot]`

The first run deliberately posts nothing else. It records your league's current position as
already-seen, so installing mid-season does not dump the season's backlog — potentially hundreds of
messages — into your channel. Everything from that point forward is posted normally.

If the run fails, see [Troubleshooting](#troubleshooting).

## Configuration reference

| Variable | Required | Default | Notes |
|---|---|---|---|
| `LEAGUE_ID` | yes | — | ESPN league id |
| `LEAGUE_YEAR` | yes | — | Season year, e.g. `2026` |
| `ESPN_S2` | yes | — | Private league cookie |
| `SWID` | yes | — | Private league cookie; braces optional |
| `DISCORD_WEBHOOK_URL` | yes | — | Reminders, startup message, error notices, and anything unrouted |
| `DISCORD_WEBHOOK_URL_TRADES` | no | main webhook | Trades |
| `DISCORD_WEBHOOK_URL_ROSTER` | no | main webhook | Waiver claims, free agent adds, drops |
| `DISCORD_WEBHOOK_URL_RESULTS` | no | main webhook | Weekly results |
| `TIMEZONE` | no | `America/Chicago` | IANA zone name |
| `LINEUP_REMINDERS` | no | `true` | `true`/`false`/`1`/`0`/`yes`/`no` |

## How it works

**Schedule.** The workflow runs every 5 minutes, September through January. Five minutes is GitHub's
minimum interval, and its scheduler drops short-interval runs first under load, so expect somewhat
fewer in practice. Nothing depends on the cadence -- deduplication is content-hashed, not timed.

The cron month field (`9-12,1`) keeps it from waking up in the offseason. Note that a league drafting
in August gets no coverage until 1 September; add `8` to the month list for draft-day transactions.

**Dynasty leagues.** Rookie drafts and offseason trades happen year round. To run continuously, edit
`.github/workflows/notifier.yml` and change the cron month field to `*`:

```yaml
- cron: "*/5 * * * *"
```

Nothing else needs changing — the script is already safe to run in the offseason.

**Deduplication.** Transactions are tracked by a watermark on the newest activity timestamp, backed by a
rolling list of ~300 content fingerprints. The fingerprints matter: two transactions can share a
millisecond, and a timestamp alone would silently drop the second one.

**Lineup reminders** are computed in your configured timezone at runtime rather than by a fixed UTC
cron. Central time shifts an hour under daylight saving in November, and a hardcoded UTC schedule would
drift without anything failing loudly.

**Failure isolation.** Transactions, weekly results, and reminders each run independently. One failing
never suppresses the other two.

**Failures are never silent.** A broken run posts a message to the webhook and exits non-zero so the
Actions run shows red. Those error messages are rate-limited to once per day, so a persistent problem
does not post 72 times.

## Annual maintenance

Two things need attention roughly once a year, both around August:

1. **Bump `LEAGUE_YEAR`** to the new season. ESPN treats each season as a separate league, so leaving
   this at last year's value means the notifier watches a league that is no longer active.
2. **Re-pull `ESPN_S2` and `SWID`.** These cookies expire roughly annually, and immediately if you
   change your ESPN password. When they expire the notifier posts a message saying so and exits
   non-zero — repeat step 2 above and update the secrets.

## Troubleshooting

**"ESPN rejected the notifier's credentials"** — the cookies have expired or your ESPN password
changed. Re-pull `ESPN_S2` and `SWID` and update the secrets.

**The channel has gone quiet** — check the Actions tab for failed runs. If runs are green and nothing is
posting, your league may genuinely have had no activity. Trigger a manual run to confirm the workflow
still works.

**The workflow stopped running on schedule** — GitHub disables scheduled workflows in repositories with
no activity for 60 days. Push any commit, or trigger a manual run, to re-enable it. Also check you have
not exhausted Actions minutes on a private repo.

**Duplicate messages** — most likely `state.json` failed to commit. Check the "Commit state" step of
recent runs for push errors.

**Reminders never arrive** — confirm `LINEUP_REMINDERS` is not set to `false`, and that `TIMEZONE` names
a real IANA zone. An invalid zone posts an error rather than failing quietly.

## Local development

Runtime dependencies are exactly two: `espn_api` and `requests`. Tests use the standard library's
`unittest` and add nothing.

```bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt   # .venv/Scripts/python.exe on Windows
```

Run the full test suite:

```bash
.venv/bin/python -m unittest discover -s tests -v
```

Preview every message type against fixture data — no network, no secrets, no state written:

```bash
.venv/bin/python poller.py --dry-run
```

Two optional dev-only packages, deliberately kept out of `requirements.txt`: `tzdata`, which `zoneinfo`
needs on Windows, and `pyyaml`, used by the workflow structure tests. Both are skipped gracefully when
absent.

## Not included

Power rankings, standings posts, Monday-night reminders, matchup previews, and per-user DMs are all out
of scope. This is a webhook-only notifier: there is no Discord bot token, no gateway connection, and no
slash commands.
