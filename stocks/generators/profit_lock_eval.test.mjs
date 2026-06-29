/* Node unit-test for the client-side +1R Profit-Lock evaluator (F profit-lock, 2026-06-24).
 * Loads the SAME assets/profit_lock_eval.js the browser loads, so logic can't drift.
 * Run: node stocks/generators/profit_lock_eval.test.mjs   (exit 0 = all pass)
 *
 * Cases (operator's verify spec):
 *   1. a trade crossing +1R -> alert fires ONCE with correct payload (and a 2nd
 *      tick with the same priorState does NOT re-fire / stays not-new).
 *   2. a trade already at breakeven (stop armed) -> SUPPRESSED (no nag).
 *   3. a 0-result run (no live prices) -> keeps last-good: no crash, no alerts,
 *      prior de-dupe state is not corrupted.
 */
import { createRequire } from "module";
import { fileURLToPath } from "url";
import path from "path";
const require = createRequire(import.meta.url);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const { computeProfitLock, PROFIT_LOCK_HIGH_R, COST_BUFFER_PCT } =
  require(path.join(HERE, "..", "assets", "profit_lock_eval.js"));

let fails = 0;
function ok(cond, label, extra) {
  console.log((cond ? "  [PASS] " : "  [FAIL] ") + label + (extra && !cond ? "  -> " + extra : ""));
  if (!cond) fails++;
}
const approx = (a, b) => Math.abs(a - b) < 1e-6;

console.log("PROFIT_LOCK_HIGH_R =", PROFIT_LOCK_HIGH_R, "· COST_BUFFER_PCT =", COST_BUFFER_PCT);

// ── Case 1: crossing +1R fires once with correct payload ────────────────────
// entry 100, invalidation 90 -> R=10, +1R level = 110, breakeven = 100.2.
// live 111 >= 110 -> fires. stop_now 90 (< BE) -> not suppressed.
const T1 = [{ trade_id: "T1", ticker: "ALPHA", status: "OPEN",
              entry_filled: 100, invalidation: 90, last_close: 109,
              live_stop: { stop_now: 90, armed: false } }];
const price1 = { ALPHA: { last: 111 } };
const NOW = "2026-06-24T06:30:00Z";

let r1 = computeProfitLock(T1, t => price1[t], {}, { nowIso: NOW });
ok(r1.alerts.length === 1, "Case1: exactly one alert fired", JSON.stringify(r1.alerts.length));
const a = r1.alerts[0] || {};
ok(a.ticker === "ALPHA", "Case1: payload ticker", a.ticker);
ok(approx(a.live, 111), "Case1: payload live price", a.live);
ok(approx(a.entry, 100), "Case1: payload entry", a.entry);
ok(approx(a.stop, 90), "Case1: payload current stop", a.stop);
ok(approx(a.oneR, 10) && approx(a.plLevel, 110) && approx(a.breakeven, 100.2),
   "Case1: 1R / +1R level / breakeven math", `${a.oneR}/${a.plLevel}/${a.breakeven}`);
ok(a.message === "ALPHA +1R intraday — Profit-Lock condition met; consider moving your stop to breakeven (entry+cost).",
   "Case1: exact advisory message", a.message);
ok(a.isNew === true, "Case1: first crossing flagged isNew", String(a.isNew));

// de-dupe: feed the returned state back in -> still surfaced (advisory banner
// persists) but NO LONGER new -> "fires once per crossing, not every tick".
let r1b = computeProfitLock(T1, t => price1[t], r1.state, { nowIso: "2026-06-24T06:35:00Z" });
ok(r1b.alerts.length === 1 && r1b.alerts[0].isNew === false, "Case1: 2nd tick same crossing does not re-fire (isNew=false)",
   JSON.stringify(r1b.alerts.map(x => x.isNew)));
ok(r1b.alerts[0].firedAt === NOW, "Case1: firedAt preserved from the original crossing", r1b.alerts[0].firedAt);

// re-cross: price falls below +1R (state clears) then crosses again -> fires anew.
let rDrop = computeProfitLock(T1, () => ({ last: 105 }), r1b.state, { nowIso: "2026-06-24T07:00:00Z" });
ok(rDrop.alerts.length === 0 && Object.keys(rDrop.state).length === 0, "Case1: drop below +1R clears fired state",
   JSON.stringify(rDrop.state));
let rRe = computeProfitLock(T1, t => price1[t], rDrop.state, { nowIso: "2026-06-24T07:30:00Z" });
ok(rRe.alerts.length === 1 && rRe.alerts[0].isNew === true, "Case1: genuine re-cross fires again (isNew=true)",
   String(rRe.alerts[0] && rRe.alerts[0].isNew));

// ── Case 2: already at breakeven (stop armed) -> suppressed ─────────────────
const T2 = [{ trade_id: "T2", ticker: "BETA", status: "OPEN",
              entry_filled: 100, invalidation: 90, last_close: 112,
              live_stop: { stop_now: 100.2, armed: true } }];
let r2 = computeProfitLock(T2, () => ({ last: 115 }), {}, { nowIso: NOW });
ok(r2.alerts.length === 0, "Case2: no alert when stop already at/above breakeven", JSON.stringify(r2.alerts));
ok(r2.suppressed.length === 1 && r2.suppressed[0].ticker === "BETA", "Case2: surfaced as suppressed (no nag)",
   JSON.stringify(r2.suppressed));
// also suppress purely on stop>=BE even if armed flag is missing:
const T2b = [{ trade_id: "T2b", ticker: "BE2", status: "OPEN", entry_filled: 100, invalidation: 90,
               live_stop: { stop_now: 101 } }];
let r2b = computeProfitLock(T2b, () => ({ last: 115 }), {}, { nowIso: NOW });
ok(r2b.alerts.length === 0 && r2b.suppressed.length === 1, "Case2: suppress on stop>=breakeven without armed flag",
   JSON.stringify(r2b));

// ── Case 3: 0-result run (no live prices) -> keeps last-good, no crash ──────
const priorState = { T1: { firedAt: NOW } };
let r3, threw = false;
try {
  r3 = computeProfitLock(T1, () => null, priorState, { nowIso: NOW, fallbackToLastClose: false });
} catch (e) { threw = true; }
ok(!threw, "Case3: no-price run does not throw");
ok(r3 && r3.alerts.length === 0, "Case3: no alerts when no live price (never fabricates)", r3 && JSON.stringify(r3.alerts));
ok(r3 && r3.skipped.some(s => s.reason === "no-live-price"), "Case3: skipped with no-live-price reason",
   r3 && JSON.stringify(r3.skipped));
ok(priorState.T1 && priorState.T1.firedAt === NOW, "Case3: prior de-dupe state untouched (last-good preserved)",
   JSON.stringify(priorState));

// malformed item alongside a good one -> good one still alerts, bad one skipped.
const MIX = [{ /* junk */ status: "OPEN" }, T1[0]];
let rMix = computeProfitLock(MIX, t => price1[t], {}, { nowIso: NOW });
ok(rMix.alerts.length === 1, "Case3: one malformed trade never blanks the good alert", JSON.stringify(rMix.alerts.length));

console.log(fails ? `\nRESULT: ${fails} FAILURE(S)` : "\nRESULT: ALL PASS");
process.exit(fails ? 1 : 0);
