# Equity Dashboard — GitHub Pages SETUP (one-time)

Walks you through getting the live equity dashboard online. Mirrors the silver dashboard
setup pattern; will live as a subfolder inside the existing `SparchoTradingDesk` repo so
GitHub Pages is already configured — you just need to upload the files.

**Outcome:** the equity dashboard accessible at
`https://sparcho.github.io/SparchoTradingDesk/stocks/`

## Prereqs (already done from silver setup)
- ✅ GitHub repo `sparcho/SparchoTradingDesk` exists and is public
- ✅ GitHub Pages enabled on the `main` branch root
- ✅ You have the repo open locally OR you're using github.com web UI

## Step 1 — Upload the `stocks/` folder

**Via GitHub web UI (easiest first time):**

1. Open https://github.com/sparcho/SparchoTradingDesk
2. Click "Add file" → "Upload files"
3. Drag the entire `equity/web/` folder from your vault path:
   `C:\Users\user\Desktop\CLAUDE PLAY\TRADER\00_SYSTEM\DASHBOARDS\equity\web`
4. **Important:** rename the uploaded folder from `web` to `stocks` before committing
   (so the URL is `/SparchoTradingDesk/stocks/` and not `/SparchoTradingDesk/web/`)
5. Commit message: `Add equity dashboard v3 (F108)`
6. Click "Commit changes"

The uploaded structure should look like this inside the repo:

```
SparchoTradingDesk/
├── index.html              (silver dashboard — already there)
├── data/                   (silver data — already there)
├── generators/             (silver emitter — already there)
├── .github/workflows/      (silver auto-refresh — already there)
└── stocks/                 ← NEW
    ├── index.html
    ├── data/
    │   └── equity_dashboard_aggregate.json
    ├── generators/
    │   └── equity_dashboard_emit.py
    └── .gitignore
```

## Step 2 — Verify it renders

1. Wait ~30 seconds after commit (GH Pages build cycle)
2. Open `https://sparcho.github.io/SparchoTradingDesk/stocks/`
3. You should see the dashboard render with current data

If you see a 404, check that the upload landed at `stocks/index.html` exactly. The URL is
case-sensitive on GitHub Pages.

## Step 3 — Bookmark + share

The URL is shareable. Same as silver, the page is fully self-contained — anyone with the
URL can view it (it's a public repo).

## Refresh cadence — see [[REFRESH.md]]

The dashboard does NOT auto-refresh from a price feed (the data lives in your private
vault — not in this public repo). You refresh manually by re-emitting the JSON and
committing it. See REFRESH.md for the one-line workflow.

## Differences from silver

| Aspect | Silver | Equity |
|---|---|---|
| Data source | YAML in repo + goldapi live | Vault `_index.md` + broker JSON (NOT in repo) |
| Auto-refresh | Yes (20min cron via GH Actions) | Manual (you push new JSON on cadence) |
| Live price | goldapi.io XAGUSD | NSE close (from operator's broker drop) |
| Refresh feedback | "X min ago" pulse pill | "Emitted at YYYY-MM-DD HH:MM" timestamp |

Why no auto-refresh for equity: the dashboard reads from per-stock `_index.md` (gates,
briefs, regime lenses) which are operator-curated in the private vault. Putting that
data in a public repo would expose your thesis IP. For v2 we can split the schema —
prices-only into the public repo updated by GH Action, thesis frames stay private — but
v1 keeps things simple.

## Repo location decision

We're using `stocks/` subfolder in the existing repo (not a new repo). Benefits:
- Reuses the GH Pages configuration
- Single GH Actions workflow can be extended later to refresh both
- Same domain — easier for father to remember

If you'd rather have a separate repo (`sparcho/EquityDashboard`), it's a 5-min rebuild —
just say.
