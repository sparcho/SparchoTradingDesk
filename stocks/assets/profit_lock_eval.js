/*!
 * profit_lock_eval.js — pure, dependency-free +1R Profit-Lock evaluator.
 *
 * THE PRIVACY-CLEAN DESIGN (operator-decided 2026-06-24): the trade ledger
 * (entry / stop / qty) is AES-GCM encrypted in the published aggregate and only
 * decrypts CLIENT-SIDE with the operator password — so the +1R Profit-Lock
 * advisory is evaluated here, in the browser, from the DECRYPTED trade_lab,
 * against the PUBLIC live price the 15-min cron already publishes (prices.json).
 * Nothing private ever leaves the browser; no public alerts JSON is written.
 *
 * This is the live same-day version of the resolver's high-arm
 * (trade_tracker_emit.py PROFIT_LOCK_HIGH_R = 1.0): a live price >= entry + 1.0R
 * arms the advisory, exactly as a session HIGH >= entry + 1R arms Profit-Lock in
 * the resolver. ADVISORY ONLY — it never acts; the operator places all trades.
 *
 * Single source of truth for the logic: the browser (index.html) AND the Node
 * unit-test (profit_lock_eval.test.mjs) both load THIS file, so they can't drift.
 *
 * GUARDS (mirror the news pipeline):
 *   - per-item try/catch: one malformed trade can never blank the whole run.
 *   - never fabricate: a trade with no live price (and no fallback) is SKIPPED,
 *     not alerted on a guessed number.
 *   - SUPPRESS if the stop is already >= breakeven (armed / moved up) — no nag.
 *   - de-dupe: fires ONCE per crossing via priorState; falling back below +1R
 *     clears the fired flag so a genuine re-cross fires again.
 *
 * computeProfitLock(openTrades, priceLookup, priorState, opts) ->
 *   { alerts, state, suppressed, skipped, asOf }
 *     alerts    : [{trade_id, ticker, live, entry, stop, oneR, plLevel,
 *                   breakeven, message, firedAt, isNew, priceStale}]
 *     state     : {trade_id: {firedAt}}  -> persist (localStorage) for de-dupe
 *     suppressed: [{trade_id, ticker, reason}]
 *     skipped   : [{trade_id, ticker, reason}]
 */
(function (root) {
  "use strict";

  var DEFAULTS = {
    highR: 1.0,          // PROFIT_LOCK_HIGH_R — align to the resolver high-arm
    costBufferPct: 0.002, // COST_BUFFER_PCT — breakeven = entry + cost
    eps: 1e-6,
    fallbackToLastClose: true, // if prices.json lacks the ticker, degrade to last_close (tagged stale)
    nowIso: null
  };

  function _fin(x) { return typeof x === "number" && isFinite(x); }

  function _num(x) {
    if (x == null) return null;
    var n = (typeof x === "number") ? x : parseFloat(x);
    return _fin(n) ? n : null;
  }

  function _isOpen(status) {
    var s = String(status || "").toUpperCase();
    return s === "OPEN" || s === "FILLED" || s === "ACTIVE";
  }

  function computeProfitLock(openTrades, priceLookup, priorState, opts) {
    opts = opts || {};
    var o = {};
    for (var k in DEFAULTS) o[k] = (opts[k] !== undefined) ? opts[k] : DEFAULTS[k];
    var nowIso = o.nowIso || new Date().toISOString();
    priorState = priorState || {};
    var look = (typeof priceLookup === "function") ? priceLookup : function () { return null; };

    var alerts = [], suppressed = [], skipped = [], state = {};
    var list = Array.isArray(openTrades) ? openTrades : [];

    for (var i = 0; i < list.length; i++) {
      var t = list[i] || {};
      var tid = String(t.trade_id != null ? t.trade_id : ("#" + i));
      var tk = String(t.ticker != null ? t.ticker : "?");
      try {
        if (!_isOpen(t.status)) { skipped.push({ trade_id: tid, ticker: tk, reason: "not-open" }); continue; }

        var entry = _num(t.entry_filled);
        if (entry == null) entry = _num(t.entry_trigger);  // planned-but-filled edge
        if (entry == null) { skipped.push({ trade_id: tid, ticker: tk, reason: "no-entry" }); continue; }

        var inval = _num(t.invalidation);
        if (inval == null) { skipped.push({ trade_id: tid, ticker: tk, reason: "no-invalidation" }); continue; }

        var oneR = entry - inval;                 // long-only risk = entry - stop
        if (oneR <= 0) { skipped.push({ trade_id: tid, ticker: tk, reason: "non-positive-R" }); continue; }

        var plLevel = entry + o.highR * oneR;       // +1R high-arm level
        var breakeven = entry * (1 + o.costBufferPct);

        // PUBLIC live price (prices.json). Degrade to last_close only if allowed; tag it.
        var pl = null; try { pl = look(tk); } catch (_e) { pl = null; }
        var live = pl ? _num(pl.last) : null;
        var priceStale = false;
        if (live == null && o.fallbackToLastClose) {
          live = _num(t.last_close);
          priceStale = (live != null);
        }
        if (live == null) { skipped.push({ trade_id: tid, ticker: tk, reason: "no-live-price" }); continue; }

        // current live stop (resolver-armed) — fall back to the original invalidation.
        var ls = t.live_stop || {};
        var stopNow = _num(ls.stop_now);
        var curStop = (stopNow != null) ? stopNow : inval;
        var armed = (ls.armed === true);

        // SUPPRESS — stop already at/above breakeven (armed or moved up): don't nag.
        if (armed || (stopNow != null && stopNow >= breakeven - o.eps)) {
          suppressed.push({ trade_id: tid, ticker: tk, reason: armed ? "armed" : "stop>=breakeven" });
          continue; // not carried into state -> fired flag clears
        }

        var met = live >= (plLevel - o.eps);
        if (!met) { continue; }  // below +1R: no alert; fired flag clears (de-dupe re-arms)

        var prior = priorState[tid];
        var hadFired = !!(prior && prior.firedAt);
        var firedAt = hadFired ? prior.firedAt : nowIso;
        state[tid] = { firedAt: firedAt };

        alerts.push({
          trade_id: tid, ticker: tk,
          live: live, entry: entry, stop: curStop,
          oneR: oneR, plLevel: plLevel, breakeven: breakeven,
          message: tk + " +1R intraday — Profit-Lock condition met; consider moving your stop to breakeven (entry+cost).",
          firedAt: firedAt, isNew: !hadFired, priceStale: priceStale
        });
      } catch (err) {
        skipped.push({ trade_id: tid, ticker: tk, reason: "error:" + (err && err.message ? err.message : String(err)) });
      }
    }

    return { alerts: alerts, state: state, suppressed: suppressed, skipped: skipped, asOf: nowIso };
  }

  var api = {
    computeProfitLock: computeProfitLock,
    PROFIT_LOCK_HIGH_R: DEFAULTS.highR,
    COST_BUFFER_PCT: DEFAULTS.costBufferPct
  };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root) root.PROFIT_LOCK = api;
})(typeof window !== "undefined" ? window : (typeof globalThis !== "undefined" ? globalThis : this));
