# Silver Dashboard — GitHub Pages Setup

> Goal: host `index.html` + `data/silver_dashboard_aggregate.json` at a public URL so your father can bookmark it on his phone/laptop and always see fresh data.

**Repo:** https://github.com/sparcho/SparchoTradingDesk
**Owner:** sparcho
**Target Pages URL (once live):** https://sparcho.github.io/SparchoTradingDesk/

---

## Phase 1 — first-time setup (one-time, ~10 min, no git CLI required)

### Step 1 — upload the initial files via the GitHub web UI

1. Open https://github.com/sparcho/SparchoTradingDesk in your browser.
2. Click **"Add file"** → **"Upload files"**.
3. From your computer, drag in **the entire `web/` folder contents** — but flatten so the files sit at repo root:
   - `index.html` → repo root
   - `data/silver_dashboard_aggregate.json` → in a `data/` folder at repo root
4. Scroll down: commit message "initial dashboard upload", click **"Commit changes"**.

**File paths on your computer:**
- `C:\Users\user\Desktop\CLAUDE PLAY\TRADER\00_SYSTEM\DASHBOARDS\silver\web\index.html`
- `C:\Users\user\Desktop\CLAUDE PLAY\TRADER\00_SYSTEM\DASHBOARDS\silver\web\data\silver_dashboard_aggregate.json`

> Easiest way: open both files in Explorer, drag-select, drop into the GitHub upload zone. GitHub preserves the `data/` subfolder structure.

### Step 2 — enable GitHub Pages

1. In the repo, click **Settings** (top tab).
2. Left sidebar: **Pages**.
3. Under "Build and deployment" → **Source:** select **"Deploy from a branch"**.
4. **Branch:** select `main` (or `master`, whichever your repo defaults to) + `/ (root)`.
5. Click **Save**.
6. GitHub will show a green banner: "Your site is live at https://sparcho.github.io/SparchoTradingDesk/". This takes ~30-90 seconds the first time.

### Step 3 — visit the URL + bookmark on father's devices

1. Open **https://sparcho.github.io/SparchoTradingDesk/** in your browser.
2. You should see the full silver dashboard rendering. (If you see "Could not load dashboard data" — the JSON didn't upload correctly; check the `data/` folder in the repo.)
3. WhatsApp the URL to your father; he bookmarks on his phone home screen + laptop browser.

### Step 4 — security decision

GitHub Pages on a public repo means **anyone with the URL can view the dashboard**. The URL is unguessable (nobody will randomly find `sparcho.github.io/SparchoTradingDesk/`) but the data is technically public.

Options:
- **A) Leave it public (recommended for v1)** — unguessable URL = practical privacy. Just don't share it widely.
- **B) Make repo private + enable GitHub Pages Pro** ($4/month) — true auth gate.
- **C) Cloudflare Access in front of the URL** — free; gates the page behind your father's Google login.

If you want B or C later, we can swap. For now, A.

---

## Phase 2 — refresh workflow (every time data changes)

### Manual refresh (no git CLI needed)

1. Run the emitter locally to regenerate the JSON:
   ```
   cd "C:\Users\user\Desktop\CLAUDE PLAY\TRADER"
   python 00_SYSTEM\GENERATORS\silver_dashboard_emit.py --publish
   ```
   This writes the JSON to both `_state/` and `00_SYSTEM/DASHBOARDS/silver/web/data/`.

2. Open https://github.com/sparcho/SparchoTradingDesk/blob/main/data/silver_dashboard_aggregate.json
3. Click the pencil ✏️ icon (top right of file view) → **"Upload files"** OR drag-replace the file via the repo root.
4. Commit. GitHub Pages auto-redeploys within ~60 seconds. Father reloads → fresh data.

### Automated refresh (once you've installed git CLI on your laptop)

1. One-time: clone the repo into your TRADER folder structure:
   ```
   cd "C:\Users\user\Desktop\CLAUDE PLAY\TRADER\00_SYSTEM\DASHBOARDS\silver"
   git clone https://github.com/sparcho/SparchoTradingDesk.git web-git
   ```
   (Renamed to `web-git/` to avoid clobbering the existing `web/` staging folder.)

2. Tell Claude to update `silver_dashboard_emit.py` to publish into `web-git/` instead of `web/`.

3. Then refreshes become a single command:
   ```
   python 00_SYSTEM\GENERATORS\silver_dashboard_emit.py --publish --git-push
   ```
   Emitter writes JSON, git adds/commits/pushes, GitHub Pages auto-redeploys.

4. Optional next step: wire as a scheduled task (`data_silver_dashboard.md`) that fires daily post-NSE-close.

---

## What I (Claude) need from you next

Now that you've created the repo, my next steps are:

1. ✅ Port HTML to fetch-JSON variant — DONE (web/index.html)
2. ✅ Add `--publish` + `--git-push` flags to emitter — DONE
3. ⏳ **You: do Phase 1 Steps 1-3 above** (web UI upload + enable Pages + visit URL)
4. ⏳ **You report back:** "URL renders" → we're done with v1.
5. ⏳ (Optional, when ready): install git CLI on your laptop, tell me, and I switch the emitter to push automatically.

**If the upload fails or the URL shows "Could not load dashboard data"**, send me a screenshot of the repo's file tree + I'll diagnose.

---

## Files in this folder

| File | Role |
|---|---|
| `index.html` | The dashboard page — fetches `data/silver_dashboard_aggregate.json` on load |
| `data/silver_dashboard_aggregate.json` | The data the page reads; regenerated by the emitter |
| `SETUP.md` | This file |

## Related

- Source of truth: `00_SYSTEM/GENERATORS/_inputs/silver_holdings.yaml` (operator edits this)
- Emitter: `00_SYSTEM/GENERATORS/silver_dashboard_emit.py`
- Cowork artifact (for Sparcho only): `silver-dashboard-v1` (uses inline-embedded JSON)
- Web version (for father): this folder
