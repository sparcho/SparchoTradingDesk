# Equity Dashboard — REFRESH workflow

How to push fresh data to the live equity dashboard at
`https://sparcho.github.io/SparchoTradingDesk/stocks/`.

## When to refresh

- After every broker drop (new `positions_unified.json` from your HDFC snapshot)
- After any per-stock `_index.md` frontmatter change (gates updated, journalist brief refreshed, etc.)
- After fresh screener runs land (`scrn_eod` daily)
- Whenever you want the dashboard to show today's truth

## The two-minute refresh

```bash
# 1. Re-emit the aggregate JSON from canonical sources
cd ~/path/to/TRADER
python3 00_SYSTEM/GENERATORS/equity_dashboard_emit.py

# 2. Copy the fresh JSON to the local repo clone
cp 00_SYSTEM/_state/equity_dashboard_aggregate.json \
   ~/path/to/SparchoTradingDesk/stocks/data/equity_dashboard_aggregate.json

# 3. Commit + push
cd ~/path/to/SparchoTradingDesk
git add stocks/data/equity_dashboard_aggregate.json
git commit -m "Equity refresh $(date +%Y-%m-%d)"
git push
```

GitHub Pages will rebuild within 30 seconds. Reload the URL.

## If you don't have the repo cloned locally

Use github.com web UI:

1. Run the emitter locally: `python3 00_SYSTEM/GENERATORS/equity_dashboard_emit.py`
2. Open https://github.com/sparcho/SparchoTradingDesk/blob/main/stocks/data/equity_dashboard_aggregate.json
3. Click the pencil icon (edit)
4. Replace the content by pasting your fresh JSON
5. Commit changes

## Verifying the refresh landed

The dashboard's title-strip shows `emitted: YYYY-MM-DD HH:MMZ`. After GH Pages rebuilds,
reload the page and that timestamp should match your latest emit.

## Want a one-command alias?

Add to your shell:

```bash
alias equity-refresh='cd ~/path/to/TRADER && python3 00_SYSTEM/GENERATORS/equity_dashboard_emit.py && cp 00_SYSTEM/_state/equity_dashboard_aggregate.json ~/path/to/SparchoTradingDesk/stocks/data/equity_dashboard_aggregate.json && cd ~/path/to/SparchoTradingDesk && git add stocks/data && git commit -m "Equity refresh $(date +%Y-%m-%d)" && git push'
```

Then `equity-refresh` from any directory.

## Auto-refresh later (v2 idea)

Two paths to consider:

1. **Split-repo refresh**: prices live in a public repo (auto-refreshed by GH Actions
   pulling NSE quotes), thesis-frames stay in private vault, dashboard merges both on load.
2. **Local cron**: a cron job on your machine runs the refresh command daily after EOD.
   Pros: keeps thesis private. Cons: requires your machine to be on.

Both are in scope when you want them — say the word.
