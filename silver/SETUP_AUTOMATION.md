# Silver Dashboard — GitHub Actions Auto-Refresh

> Goal: dashboard refreshes **every 20 minutes** during global silver-trading hours, **hourly** off-hours, with **zero manual intervention**. Fully serverless — runs on GitHub's infrastructure.

**Repo:** https://github.com/sparcho/SparchoTradingDesk

---

## Architecture

```
GitHub Actions cron (every 20 min trading hours, hourly weekends)
    ↓
runs generators/silver_dashboard_emit.py on GitHub's Ubuntu runner
    ↓
emitter fetches:
  - goldapi.io   → XAGUSD live spot (24/5; uses GOLDAPI_KEY secret)
  - Yahoo        → SILVERBEES NSE close, USDINR, DXY, Gold (free, no key)
  - reads        → _inputs/silver_holdings.yaml (holdings + narrative)
    ↓
writes data/silver_dashboard_aggregate.json
    ↓
git commit + git push (only if data actually changed)
    ↓
GitHub Pages auto-redeploys within ~60s
    ↓
Father reloads sparcho.github.io/SparchoTradingDesk/ → fresh data
```

**Quota math:**
- Weekday: cron fires every 20 min between 03:00-22:00 UTC = ~60 fires/day
- Weekend: hourly = 24 fires/day
- Each fire = 1 goldapi.io call → ~60/day weekday, 24/day weekend
- **goldapi.io free tier: 100/day** → safely under cap

---

## Phase 1 — get goldapi.io API key (~5 min)

1. Open https://www.goldapi.io/
2. Sign up with email (no credit card needed for free tier)
3. Verify email → log in
4. Dashboard shows your API key (format: `goldapi-xxxxx-xxxxx`). Copy it.

**Free tier confirmed:** 100 requests/day, real-time XAU/XAG/PA/PT.

---

## Phase 2 — add the API key as a GitHub repo secret (~2 min)

1. Open https://github.com/sparcho/SparchoTradingDesk/settings/secrets/actions
2. Click **"New repository secret"**
3. **Name:** `GOLDAPI_KEY` (must be exactly this)
4. **Value:** paste your goldapi.io key
5. Click **"Add secret"**

The key is now available to GitHub Actions but never visible in commits or logs.

---

## Phase 3 — upload the automation files to the repo (~5 min)

You need to add these new files/folders to your repo. Two paths:

### Path A — drag-and-drop via web UI (no git CLI)

Open https://github.com/sparcho/SparchoTradingDesk/ → click **"Add file"** → **"Upload files"** → drag in these folders/files from your computer:

| Local path | Repo path |
|---|---|
| `00_SYSTEM\DASHBOARDS\silver\web\generators\` (whole folder, 2 .py files) | `generators/` |
| `00_SYSTEM\DASHBOARDS\silver\web\_inputs\silver_holdings.yaml` | `_inputs/silver_holdings.yaml` |
| `00_SYSTEM\DASHBOARDS\silver\web\_cache\daily_prices.csv` | `_cache/daily_prices.csv` |
| `00_SYSTEM\DASHBOARDS\silver\web\.github\workflows\refresh-dashboard.yml` | `.github/workflows/refresh-dashboard.yml` |

> ⚠️ **Skip the `generators/__pycache__/` folder if present** — it's just Python compiler cache, not needed. Drag only `silver_dashboard_emit.py` + `yahoo_common.py` from inside `generators/`.

> **Easiest:** in Windows Explorer, select all four top-level folders (`.github`, `_cache`, `_inputs`, `generators`) inside `00_SYSTEM\DASHBOARDS\silver\web\`, drag the whole selection onto the GitHub upload zone. GitHub preserves folder structure including the `.github/workflows/` nested path.

Commit message: "add automation pipeline". Click **"Commit changes"**.

### Path B — git CLI (skip for now; can switch later)

If you ever install git locally and clone the repo, you can do this by just `git add . && git commit && git push`. Same result.

---

## Phase 4 — verify the workflow runs (~3 min)

1. Open https://github.com/sparcho/SparchoTradingDesk/actions
2. You should see "Refresh silver dashboard" listed. If it didn't auto-fire on your push, click **"Run workflow"** → green dropdown → **"Run workflow"** to trigger manually.
3. Watch the run — should turn green ✅ within ~30 seconds.
4. After it completes, check the commit history: https://github.com/sparcho/SparchoTradingDesk/commits/main
5. You should see a new commit by `github-actions[bot]` updating `data/silver_dashboard_aggregate.json`.
6. Open the live URL: https://sparcho.github.io/SparchoTradingDesk/
7. **XAGUSD live cell should now show the goldapi.io value** (and the 📌 pin tooltip should say `goldapi.io free tier — 24/5 OTC spot`).

If the workflow fails, the Actions tab shows the error log. Most likely cause: `GOLDAPI_KEY` secret not set or misspelled.

---

## Phase 5 — clear the manual override (so live data wins again)

Right now `_inputs/silver_holdings.yaml` has `live_xagusd_override: 75.80` (your Sunday-evening pin). Once goldapi.io is wired and confirmed working, set this to `null` so live data takes over.

Quickest way:
1. Open https://github.com/sparcho/SparchoTradingDesk/blob/main/_inputs/silver_holdings.yaml
2. Click the pencil ✏️ icon (top right of file view) → edit in browser
3. Find the line `live_xagusd_override: 75.80` (around line 40)
4. Change to: `live_xagusd_override: null`
5. Commit. Workflow auto-fires on YAML push → fresh JSON → live cell now shows real goldapi spot price.

You only need to set the override again if goldapi gives bad data and you want to pin a value yourself.

---

## What you'll get after all this

- Dashboard always shows fresh data — no manual JSON uploads ever again
- Refresh cadence: every 20 min during active trading; hourly otherwise
- All from GitHub's free tier (Actions + Pages + goldapi free) → **₹0/month**
- Future YAML edits (forecast verdict, new tranches, etc.) auto-rebuild within seconds

---

## Refresh workflow going forward

**For data-only refresh:** nothing — happens automatically.

**For holdings/narrative updates:**
1. Edit `_inputs/silver_holdings.yaml` in the GitHub web UI
2. Commit
3. Workflow auto-fires on YAML change → JSON regenerated → dashboard reflects within ~60s

OR (once we wire the bridge):
1. Edit YAML in your TRADER folder via Claude (in this Cowork session)
2. I copy the change to your local `web/_inputs/` clone
3. You sync to GitHub (manual upload or git push)

---

## Files in this folder

| File | Role |
|---|---|
| `index.html` | dashboard page (fetches `data/silver_dashboard_aggregate.json`) |
| `data/silver_dashboard_aggregate.json` | dashboard data (auto-refreshed by workflow) |
| `_inputs/silver_holdings.yaml` | operator-edited source of truth |
| `_cache/daily_prices.csv` | seeded NSE history (workflow doesn't refresh this; sufficient for SBees close) |
| `generators/silver_dashboard_emit.py` | the emitter |
| `generators/yahoo_common.py` | shared Yahoo fetch helper |
| `.github/workflows/refresh-dashboard.yml` | scheduled cron + run logic |
| `SETUP.md` | original Phase 1-style web-UI setup |
| `SETUP_AUTOMATION.md` | this file |

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Workflow fails with "GOLDAPI_KEY not set" | Secret missing or misspelled | Re-add secret per Phase 2 |
| Workflow fails with `403` from goldapi | Daily quota exhausted (>100 calls) | Wait ~24h; reduce cron frequency in workflow YAML |
| Dashboard shows old XAGUSD even after workflow ran | Override still set in YAML | Phase 5 — clear `live_xagusd_override` |
| Commit happens but dashboard doesn't update | GitHub Pages cache | Hard-refresh (Ctrl+Shift+R) or wait ~60s |
| Workflow doesn't fire on schedule | GitHub disables actions on inactive repos after 60 days | Push any commit to "wake" the repo |
