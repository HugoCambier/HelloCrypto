"""Behavior lessons — what the agent *actually* did vs what played out.

Where ``playbook.py`` answers "given this signal in this regime, what's the
edge?" (market-derived, decision-maker-agnostic), this module answers
"given this regime, what is *this agent's* track record?" — extracted
directly from the ``trades`` table joined with ``price_snapshots``.

The two views are complementary:
  - Playbook: the universe of theoretical edges
  - Behavior: which edges this agent has actually been able to capture

Both get injected into the decision prompt so the model can compare its
own history to the broader signal landscape.
"""
from __future__ import annotations

import json
import logging
import os
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from statistics import mean

log = logging.getLogger(__name__)

_DATABASE_URL = os.getenv("DATABASE_URL", "")
_USE_POSTGRES = _DATABASE_URL.startswith(("postgresql://", "postgres://"))

# Sample-size floor — below this we don't emit a behavior line for that
# (action, regime) cell. Without a floor the early days of a fresh agent
# would surface noise as "lessons".
DEFAULT_MIN_SAMPLES = 5
DEFAULT_HORIZONS_H  = (24, 72)

# Threshold above which a HOLD is considered a *missed opportunity* — the
# LLM saw a strong signal (score ≥ this) but chose not to act. Mirrors
# the buy threshold used in prompts.py (≥7 for medium risk profiles).
MISSED_OPP_SCORE_MIN = 7

# Confidence buckets for calibration. Below 0.4 we skip — those are
# explicit "I'm not sure" decisions, not worth calibrating.
CONFIDENCE_BUCKETS = [(0.4, 0.5), (0.5, 0.6), (0.6, 0.7), (0.7, 0.8), (0.8, 0.9), (0.9, 1.01)]


# ── Trade loading ─────────────────────────────────────────────────────────────

def _normalize_action(a: str) -> str:
    """Map the verbose action strings stored by the agent to BUY/SELL/HOLD."""
    if not a:
        return "UNKNOWN"
    a_upper = a.upper()
    if "BUY" in a_upper:
        return "BUY"
    if "SELL" in a_upper:
        return "SELL"
    if "HOLD" in a_upper:
        return "HOLD"
    return a_upper


def _hour_floor(ts: str) -> str:
    """Truncate an ISO timestamp to the start of its hour (UTC)."""
    dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.replace(minute=0, second=0, microsecond=0)
    return dt.isoformat()


def load_decisions(mode: str | None = None, limit: int = 5000) -> list[dict]:
    """Load LLM decisions from ``market_analyses`` and flatten each row's
    actions JSON into individual (timestamp, symbol, type, score, confidence)
    records. One DB row → typically 2-10 action records.

    This is the data source for HOLD-based learning (missed opportunities)
    and confidence calibration — angles invisible to the ``trades`` table
    which only sees executed BUY/SELL.
    """
    if _USE_POSTGRES:
        from db.store import _postgres
        with _postgres() as c:
            if mode:
                c.execute("SELECT timestamp, mode, analyses FROM market_analyses WHERE mode=%s ORDER BY timestamp ASC LIMIT %s", (mode, limit))
            else:
                c.execute("SELECT timestamp, mode, analyses FROM market_analyses ORDER BY timestamp ASC LIMIT %s", (limit,))
            rows = c.fetchall()
    else:
        from db.store import _sqlite
        with _sqlite() as c:
            if mode:
                rows = c.execute("SELECT timestamp, mode, analyses FROM market_analyses WHERE mode=? ORDER BY timestamp ASC LIMIT ?", (mode, limit)).fetchall()
            else:
                rows = c.execute("SELECT timestamp, mode, analyses FROM market_analyses ORDER BY timestamp ASC LIMIT ?", (limit,)).fetchall()

    out: list[dict] = []
    for r in rows:
        d = dict(r)
        try:
            actions = json.loads(d["analyses"] or "[]")
        except Exception:
            continue
        if not isinstance(actions, list):
            continue
        ts = d["timestamp"]
        mode_v = d.get("mode")
        for a in actions:
            if not isinstance(a, dict):
                continue
            a_type = (a.get("type") or "").upper()
            if a_type not in ("BUY", "SELL", "HOLD"):
                continue
            sym = a.get("symbol")
            if not sym:
                continue
            out.append({
                "timestamp":  ts,
                "mode":       mode_v,
                "type":       a_type,
                "symbol":     sym,
                "score":      a.get("score"),
                "confidence": a.get("confidence"),
                "horizon":    a.get("horizon"),
                "reason":     a.get("reason"),
            })
    return out


def load_trades(mode: str | None = None, limit: int = 5000) -> list[dict]:
    """Load trades from DB (Postgres or SQLite)."""
    if _USE_POSTGRES:
        from db.store import _postgres
        with _postgres() as c:
            if mode:
                c.execute("SELECT * FROM trades WHERE mode=%s ORDER BY timestamp ASC LIMIT %s", (mode, limit))
            else:
                c.execute("SELECT * FROM trades ORDER BY timestamp ASC LIMIT %s", (limit,))
            return [dict(r) for r in c.fetchall()]
    from db.store import _sqlite
    with _sqlite() as c:
        if mode:
            rows = c.execute("SELECT * FROM trades WHERE mode=? ORDER BY timestamp ASC LIMIT ?", (mode, limit)).fetchall()
        else:
            rows = c.execute("SELECT * FROM trades ORDER BY timestamp ASC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── Counterfactual price lookup ───────────────────────────────────────────────

def _build_price_index(symbols: set[str]) -> dict:
    """Return {symbol: {hour_floored_ts: close}} for the listed symbols.

    Loaded once per analysis run so we don't requery for each trade.
    """
    if not symbols:
        return {}
    placeholders_ph = "%s" if _USE_POSTGRES else "?"
    in_clause = ",".join([placeholders_ph] * len(symbols))
    sql = f"SELECT symbol, timestamp, close FROM price_snapshots WHERE symbol IN ({in_clause}) ORDER BY symbol, timestamp ASC"
    if _USE_POSTGRES:
        from db.store import _postgres
        with _postgres() as c:
            c.execute(sql, tuple(symbols))
            rows = c.fetchall()
    else:
        from db.store import _sqlite
        with _sqlite() as c:
            rows = c.execute(sql, tuple(symbols)).fetchall()
    index: dict[str, dict[str, float]] = defaultdict(dict)
    for r in rows:
        d = dict(r)
        ts_floor = _hour_floor(d["timestamp"])
        index[d["symbol"]][ts_floor] = float(d["close"])
    return index


def _lookup_price_at(index: dict, symbol: str, ts_floor: str) -> float | None:
    return index.get(symbol, {}).get(ts_floor)


def _ts_offset_floor(ts_floor: str, hours: int) -> str:
    dt = datetime.fromisoformat(ts_floor) + timedelta(hours=hours)
    return dt.isoformat()


# ── Regime lookup ─────────────────────────────────────────────────────────────

def _build_regime_index(symbols: set[str]) -> dict:
    """Return {symbol: {hour_floored_ts: (regime_fng, regime_btc_trend)}}.

    We piggyback on BTCUSDC's regime tags (which depend on F&G and BTC trend,
    both macro signals) so a single index covers all symbols at a given hour.
    """
    sql = (
        "SELECT timestamp, regime_fng, regime_btc_trend "
        "FROM price_snapshots WHERE symbol='BTCUSDC' ORDER BY timestamp ASC"
    )
    if _USE_POSTGRES:
        from db.store import _postgres
        with _postgres() as c:
            c.execute(sql)
            rows = c.fetchall()
    else:
        from db.store import _sqlite
        with _sqlite() as c:
            rows = c.execute(sql).fetchall()
    out: dict[str, tuple[str | None, str | None]] = {}
    for r in rows:
        d = dict(r)
        ts_floor = _hour_floor(d["timestamp"])
        out[ts_floor] = (d.get("regime_fng"), d.get("regime_btc_trend"))
    return out


# ── Aggregation ───────────────────────────────────────────────────────────────

def _attach_horizons(
    item_ts_floor: str,
    sym: str,
    price_idx: dict,
    horizons_h: tuple[int, ...],
) -> dict[int, float | None]:
    """Compute counterfactual % returns at each horizon from snapshot prices."""
    entry = _lookup_price_at(price_idx, sym, item_ts_floor)
    if not entry:
        return {h: None for h in horizons_h}
    out: dict[int, float | None] = {}
    for h in horizons_h:
        future_ts    = _ts_offset_floor(item_ts_floor, h)
        future_price = _lookup_price_at(price_idx, sym, future_ts)
        out[h] = ((future_price - entry) / entry * 100) if future_price else None
    return out


def _compute_trade_behavior(
    trades: list[dict],
    price_idx: dict,
    regime_idx: dict,
    horizons_h: tuple[int, ...],
    min_samples: int,
) -> dict:
    """{regime: {BUY|SELL: {n, hN_mean_pct, hN_win_rate, realized_pnl_mean_pct}}}."""
    by_cell: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for t in trades:
        sym = t.get("symbol")
        if not sym:
            continue
        ts_floor = _hour_floor(t["timestamp"])
        regime_pair = regime_idx.get(ts_floor)
        if not regime_pair or not regime_pair[0] or not regime_pair[1]:
            continue
        regime_key = f"{regime_pair[0]}+{regime_pair[1]}"
        action_key = _normalize_action(t.get("action", ""))
        if action_key not in ("BUY", "SELL"):
            continue
        horizons = _attach_horizons(ts_floor, sym, price_idx, horizons_h)
        entry    = _lookup_price_at(price_idx, sym, ts_floor)
        if not entry:
            continue
        by_cell[(action_key, regime_key)].append({
            "symbol":   sym,
            "horizons": horizons,
            "pnl_pct":  (t.get("pnl") / (t.get("amount") or entry) * 100) if t.get("pnl") else None,
        })

    out: dict[str, dict] = defaultdict(dict)
    for (action, regime), items in by_cell.items():
        if len(items) < min_samples:
            continue
        cell: dict = {"n": len(items)}
        for h in horizons_h:
            vals = [it["horizons"][h] for it in items if it["horizons"].get(h) is not None]
            if vals:
                cell[f"h{h}_mean_pct"] = round(mean(vals), 3)
                cell[f"h{h}_win_rate"] = round(
                    sum(1 for v in vals if (v > 0 if action == "BUY" else v < 0)) / len(vals), 3
                )
        pnls = [it["pnl_pct"] for it in items if it.get("pnl_pct") is not None]
        if pnls:
            cell["realized_pnl_mean_pct"] = round(mean(pnls), 3)
        out[regime][action] = cell
    return dict(out)


def _compute_missed_opps(
    decisions: list[dict],
    price_idx: dict,
    regime_idx: dict,
    horizons_h: tuple[int, ...],
    min_samples: int,
    score_min: int = MISSED_OPP_SCORE_MIN,
) -> dict:
    """{regime: {n, avg_score, avg_confidence, h24_mean_pct, win_rate_if_bought}}.

    Captures HOLD decisions where the LLM saw a high score (≥ ``score_min``)
    but chose not to act. Compares the *would-have-been* return if a BUY had
    been executed at that timestamp. A positive h24 mean = the agent
    systematically passed on profitable setups in that regime.
    """
    by_regime: dict[str, list] = defaultdict(list)
    for d in decisions:
        if d["type"] != "HOLD":
            continue
        score = d.get("score")
        if score is None or score < score_min:
            continue
        ts_floor = _hour_floor(d["timestamp"])
        regime_pair = regime_idx.get(ts_floor)
        if not regime_pair or not regime_pair[0] or not regime_pair[1]:
            continue
        regime_key = f"{regime_pair[0]}+{regime_pair[1]}"
        horizons   = _attach_horizons(ts_floor, d["symbol"], price_idx, horizons_h)
        by_regime[regime_key].append({
            "score":      score,
            "confidence": d.get("confidence"),
            "horizons":   horizons,
        })

    out: dict[str, dict] = {}
    for regime, items in by_regime.items():
        if len(items) < min_samples:
            continue
        scores = [it["score"] for it in items if it.get("score") is not None]
        confs  = [it["confidence"] for it in items if it.get("confidence") is not None]
        cell: dict = {
            "n":              len(items),
            "avg_score":      round(mean(scores), 2) if scores else None,
            "avg_confidence": round(mean(confs), 2) if confs else None,
        }
        for h in horizons_h:
            vals = [it["horizons"][h] for it in items if it["horizons"].get(h) is not None]
            if vals:
                cell[f"h{h}_mean_pct"]   = round(mean(vals), 3)
                cell[f"h{h}_win_rate"]   = round(sum(1 for v in vals if v > 0) / len(vals), 3)
        out[regime] = cell
    return out


def _compute_confidence_calibration(
    decisions: list[dict],
    price_idx: dict,
    horizons_h: tuple[int, ...],
    min_samples: int = 10,
) -> dict:
    """{action: {bucket_label: {n, win_rate, predicted_mean, gap}}}.

    Compares the LLM's stated confidence to the realized outcome at h24:
      - For BUY: 'win' = price rose
      - For SELL: 'win' = price dropped
      - For HOLD: 'win' = price was approximately flat (±0.5% in 24h)

    ``gap`` = predicted (midpoint of confidence bucket) − realized win rate.
    Positive gap = overconfidence; negative = underconfidence.
    """
    by_action: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

    def bucket_for(conf: float) -> str | None:
        for lo, hi in CONFIDENCE_BUCKETS:
            if lo <= conf < hi:
                return f"{lo:.1f}-{hi:.1f}"
        return None

    h_primary = horizons_h[0]
    for d in decisions:
        conf = d.get("confidence")
        if conf is None or conf < 0.4:
            continue
        bucket = bucket_for(conf)
        if not bucket:
            continue
        ts_floor = _hour_floor(d["timestamp"])
        horizons = _attach_horizons(ts_floor, d["symbol"], price_idx, (h_primary,))
        ret = horizons.get(h_primary)
        if ret is None:
            continue
        action = d["type"]
        if action == "BUY":
            win = 1.0 if ret > 0 else 0.0
        elif action == "SELL":
            win = 1.0 if ret < 0 else 0.0
        else:  # HOLD: flat is good
            win = 1.0 if abs(ret) < 0.5 else 0.0
        by_action[action][bucket].append(win)

    out: dict[str, dict] = {}
    for action, buckets in by_action.items():
        cells: dict[str, dict] = {}
        for label, wins in buckets.items():
            if len(wins) < min_samples:
                continue
            lo_str, hi_str = label.split("-")
            lo, hi = float(lo_str), float(hi_str)
            predicted = (lo + hi) / 2
            realized  = sum(wins) / len(wins)
            cells[label] = {
                "n":              len(wins),
                "predicted_mean": round(predicted, 2),
                "realized_win":   round(realized, 3),
                "gap":            round(predicted - realized, 3),
            }
        if cells:
            out[action] = cells
    return out


def compute_behavior(
    mode: str | None = "simulation",
    horizons_h: tuple[int, ...] = DEFAULT_HORIZONS_H,
    min_samples: int = DEFAULT_MIN_SAMPLES,
) -> dict:
    """End-to-end behavior report — trades, missed opportunities, confidence calibration.

    The three views are merged into one report consumed by
    ``format_behavior_section`` for the decision prompt.
    """
    trades    = load_trades(mode=mode)
    decisions = load_decisions(mode=mode)
    if not trades and not decisions:
        return {"by_regime": {}, "confidence_calibration": {}, "n_trades": 0, "n_decisions": 0}

    # Build shared indices once
    all_symbols = {t["symbol"] for t in trades if t.get("symbol")} \
                 | {d["symbol"] for d in decisions if d.get("symbol")}
    price_idx  = _build_price_index(all_symbols)
    regime_idx = _build_regime_index(all_symbols)

    trade_view = _compute_trade_behavior(trades, price_idx, regime_idx, horizons_h, min_samples)
    missed     = _compute_missed_opps(decisions, price_idx, regime_idx, horizons_h, min_samples)
    calibration = _compute_confidence_calibration(decisions, price_idx, horizons_h)

    # Merge trade_view and missed_opps into the same {regime: {...}} dict
    by_regime: dict[str, dict] = defaultdict(dict)
    for regime, cell in trade_view.items():
        by_regime[regime].update(cell)
    for regime, cell in missed.items():
        by_regime[regime]["MISSED_OPPS"] = cell

    return {
        "by_regime":              dict(by_regime),
        "confidence_calibration": calibration,
        "n_trades":               len(trades),
        "n_decisions":            len(decisions),
        "min_samples":            min_samples,
        "missed_opp_score_min":   MISSED_OPP_SCORE_MIN,
    }


# ── Prompt-ready formatter ────────────────────────────────────────────────────

def format_behavior_section(report: dict, regime: str, max_lines: int = 6) -> str:
    """Return a compact behavior summary for the given regime (or '').

    Includes per-regime executed BUY/SELL outcomes and missed opportunities.
    Returns '' when the regime has no actionable behavior stats — surfacing
    only a generic calibration hint creates noise and double-counts with
    the post-LLM ``calibrate_confidence`` step in ``strategy``.
    """
    slot = report.get("by_regime", {}).get(regime, {})
    if not slot:
        return ""

    n_trades    = report.get("n_trades", 0)
    n_decisions = report.get("n_decisions", 0)
    lines = [f"COMPORTEMENT PASSÉ pour [{regime}] (sur {n_trades} trades, {n_decisions} décisions LLM):"]

    # ── Executed BUY / SELL ────────────────────────────────────────────
    for action in ("BUY", "SELL"):
        cell = slot.get(action)
        if not cell:
            continue
        bits = [f"n={cell['n']}"]
        if "h24_mean_pct" in cell:
            bits.append(f"h24 {cell['h24_mean_pct']:+.2f}% (win {cell['h24_win_rate']*100:.0f}%)")
        if "realized_pnl_mean_pct" in cell:
            bits.append(f"PnL réalisé moyen {cell['realized_pnl_mean_pct']:+.2f}%")
        if action == "BUY":
            interp = "rentables" if cell.get("h24_mean_pct", 0) > 0.2 else "douteux (edge faible)"
        else:
            h24 = cell.get("h24_mean_pct", 0)
            interp = "à temps" if h24 < -0.1 else ("précoces (laissés sur la table)" if h24 > 0.5 else "neutres")
        lines.append(f"  Tes {action}s {interp}: " + ", ".join(bits))

    # ── Missed opportunities (high-score HOLDs) ────────────────────────
    miss = slot.get("MISSED_OPPS")
    if miss:
        h24 = miss.get("h24_mean_pct")
        wr  = miss.get("h24_win_rate")
        score_min = report.get("missed_opp_score_min", MISSED_OPP_SCORE_MIN)
        if h24 is not None and wr is not None:
            interp = (
                f"laissés +{h24:.2f}% en moyenne ({wr*100:.0f}% auraient été gagnants)"
                if h24 > 0.2 else
                f"non rentables ({h24:+.2f}% en moyenne) — décisions de HOLD justifiées"
            )
            lines.append(
                f"  Occasions ratées (HOLDs score≥{score_min}): n={miss['n']}, "
                f"score moyen {miss.get('avg_score','?')}, conf {miss.get('avg_confidence','?')} — {interp}"
            )

    # No actionable per-regime stats made it through (just the header) →
    # suppress to keep the prompt clean. Calibration is intentionally NOT
    # surfaced here — it's already applied post-LLM via
    # ``calibrate_confidence`` in ``strategy.apply_paper_actions``, and
    # showing it would double-correct.
    if len(lines) <= 1:
        return ""

    return "\n".join(lines[:max_lines + 1])


# ── Confidence calibration (applied at decision time) ────────────────────────

# Minimum samples per (action, bucket) before the calibration kicks in. Below
# this we pass-through the raw confidence — too few trades to claim a bias.
CALIBRATION_MIN_SAMPLES = 20

# Shrinkage prior strength. The Bayesian update is:
#   alpha = n / (n + PRIOR_STRENGTH)
#   calibrated = alpha * realized + (1 - alpha) * predicted
# With PRIOR_STRENGTH=50: at n=20 alpha≈0.29, at n=100 alpha≈0.67, at n=500 alpha≈0.91.
# Translation: the calibration is conservative for thin data and trusts the
# realized win-rate more as samples grow.
CALIBRATION_PRIOR_STRENGTH = 50


def _bucket_label_for(conf: float) -> str | None:
    """Same bucketing logic as _compute_confidence_calibration. Returns None if out of range."""
    for lo, hi in CONFIDENCE_BUCKETS:
        if lo <= conf < hi:
            return f"{lo:.1f}-{hi:.1f}"
    return None


def calibrate_confidence(
    action_type: str,
    raw_confidence: float,
    calibration: dict | None,
) -> float:
    """Return a calibrated confidence value, bayesian-shrunken to history.

    Only applies to ``BUY`` actions: those are the ones gated by
    ``min_confidence``. HOLD calibration was found to be noisy (the
    "win = flat" definition is poorly suited to crypto) so we don't touch
    HOLDs here. SELL calibration would be useful too but live data is
    currently too thin to be reliable.

    Pass-through (returns ``raw_confidence`` unchanged) when:
      - The action is not BUY
      - No calibration data exists yet (cold start)
      - The matching bucket has fewer than ``CALIBRATION_MIN_SAMPLES``
    """
    if action_type.upper() != "BUY":
        return raw_confidence
    if not calibration:
        return raw_confidence
    bucket_data = calibration.get("BUY", {})
    if not bucket_data:
        return raw_confidence
    label = _bucket_label_for(raw_confidence)
    if not label or label not in bucket_data:
        return raw_confidence
    cell = bucket_data[label]
    n = cell.get("n", 0)
    if n < CALIBRATION_MIN_SAMPLES:
        return raw_confidence
    realized  = cell.get("realized_win")
    predicted = cell.get("predicted_mean")
    if realized is None or predicted is None:
        return raw_confidence
    alpha = n / (n + CALIBRATION_PRIOR_STRENGTH)
    calibrated = alpha * realized + (1 - alpha) * predicted
    # Bound to [0, 1] for downstream safety. (Predicted is already in [0, 1].)
    return max(0.0, min(1.0, calibrated))


# ── Persistence + cache for the decision cycle ────────────────────────────────

def save_behavior(report: dict) -> None:
    """Persist the behavior report in DB (``agent_state["behavior"]``)."""
    try:
        from db.store import set_state
        set_state("behavior", report)
    except Exception:
        log.exception("save_behavior: DB write failed")


def load_behavior() -> dict | None:
    try:
        from db.store import get_state
        return get_state("behavior")
    except Exception:
        log.debug("load_behavior: DB read failed", exc_info=True)
        return None


_BEHAVIOR_CACHE: dict | None = None
_BEHAVIOR_LAST_CHECK: float = 0.0
_BEHAVIOR_REVALIDATE_SEC = 60.0


def _cached_behavior() -> dict | None:
    global _BEHAVIOR_CACHE, _BEHAVIOR_LAST_CHECK
    import time
    now = time.time()
    if _BEHAVIOR_CACHE is not None and now - _BEHAVIOR_LAST_CHECK < _BEHAVIOR_REVALIDATE_SEC:
        return _BEHAVIOR_CACHE
    _BEHAVIOR_CACHE = load_behavior()
    _BEHAVIOR_LAST_CHECK = now
    return _BEHAVIOR_CACHE


def section_for_cycle(fear_greed: dict | None, market_raw: dict | None) -> str:
    """Convenience entry point matching ``playbook.section_for_cycle``.

    Returns the formatted behavior section for the current regime, or ''
    if no report exists yet (cold start) or the regime has no data.
    """
    report = _cached_behavior()
    if not report:
        return ""
    from .playbook import current_regime
    btc_trend_1d = market_raw.get("BTCUSDC", {}).get("trend_1d") if market_raw else None
    regime = current_regime(fear_greed, btc_trend_1d)
    return format_behavior_section(report, regime)


# ── CLI ───────────────────────────────────────────────────────────────────────

def _main() -> int:
    import argparse
    import sys
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", default="simulation", choices=("simulation", "real", ""))
    parser.add_argument("--min-samples", type=int, default=DEFAULT_MIN_SAMPLES)
    parser.add_argument("--out", default="")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    report = compute_behavior(mode=args.mode or None, min_samples=args.min_samples)
    payload = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if args.out:
        from pathlib import Path
        Path(args.out).write_text(payload)
        log.info("Report written to %s", args.out)
    else:
        sys.stdout.write(payload + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
