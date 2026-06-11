"""Backtest routes."""
from __future__ import annotations

import itertools
import json
import logging
import os
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request

from .. import backtest as bt_engine
from ..api import load_config

bp  = Blueprint("backtest", __name__)
log = logging.getLogger(__name__)

# Vercel kills the function process once the HTTP response is sent, so the
# daemon thread a backtest runs in is terminated mid-run and never reaches
# _persist_last_backtest — the run silently never completes. Unlike the
# simulation (delegated to the cron runner), a backtest can't move there: it
# fetches klines straight from api.binance.com, and the GitHub Actions runner
# is Binance-blocked (451). So the backtest is a local-only dev tool; in
# serverless we refuse the launch with a clear message instead of spawning a
# thread that will be reaped.
_IS_SERVERLESS = bool(os.getenv("VERCEL"))
_SERVERLESS_MSG = (
    "Le backtest n'est disponible qu'en local : l'environnement serverless "
    "(Vercel) ne peut pas exécuter un run long en arrière-plan. Lance-le "
    "depuis ton poste de dev."
)

_bt_lock       = threading.Lock()
_bt_stop_event = threading.Event()
_bt_state: dict = {"running": False, "loading": False, "snapshot": None}

# DB key holding the last completed backtest (snapshot + params + completed_at).
# Lets the cockpit + chart survive a Vercel instance recycle: the final run
# is reloaded from the agent_state table when in-memory _bt_state is empty.
_LAST_BACKTEST_KEY = "last_backtest_state"

# In-process cache for the persisted snapshot so a tab left open on /backtest
# after a run finishes doesn't fetch a multi-hundred-KB JSON blob from
# agent_state on every poll. Invalidated whenever a new run completes.
_last_backtest_cache: dict | None = None


def _persist_last_backtest(snapshot: dict, params: dict | None) -> None:
    """Save the final backtest snapshot to the DB so a page reload after a
    Vercel cold-start still shows the previous run's results."""
    global _last_backtest_cache
    try:
        from db.store import set_state
        payload = {
            "snapshot":     snapshot,
            "params":       params,
            "completed_at": datetime.utcnow().isoformat(),
        }
        set_state(_LAST_BACKTEST_KEY, json.dumps(payload))
        _last_backtest_cache = payload
    except Exception:
        log.warning("Could not persist last backtest snapshot", exc_info=True)


def _load_last_backtest() -> dict | None:
    """Inverse of _persist_last_backtest; returns None on miss.

    First call after a cold start hits agent_state (the persisted snapshot can
    be ~100-500 KB); subsequent calls hit the in-process cache. Without this,
    a backtest page left open on a finished run was burning Supabase egress
    every time the polling raced past the in-memory state.
    """
    global _last_backtest_cache
    if _last_backtest_cache is not None:
        return _last_backtest_cache
    try:
        from db.store import get_state
        raw = get_state(_LAST_BACKTEST_KEY)
        if not raw:
            return None
        parsed = json.loads(raw) if isinstance(raw, str) else raw
        _last_backtest_cache = parsed
        return parsed
    except Exception:
        return None


def _resolve_bt_state() -> tuple[dict, dict | None]:
    """Return (state, persisted) where state is the live in-memory _bt_state
    and persisted is the agent_state fallback (None if not needed)."""
    with _bt_lock:
        state = dict(_bt_state)
    if state.get("running") or state.get("snapshot"):
        return state, None
    persisted = _load_last_backtest()
    return state, persisted


@bp.get("/api/backtest/status")
def bt_status():
    """Lightweight status — progress, params, error. NO snapshot body.

    The snapshot (history, timeseries, positions) can be 100-500 KB and the
    front polls this endpoint every 3s. Returning the snapshot on every poll
    wasted ~80 KB/poll once the run accumulated trades. The full snapshot is
    now served by ``/api/backtest/snapshot`` (called once at run end + on
    explicit refresh).
    """
    state, persisted = _resolve_bt_state()
    snap = state.get("snapshot") or (persisted.get("snapshot") if persisted else None) or {}
    return jsonify({
        "running":      state.get("running", False),
        "loading":      state.get("loading", False),
        "params":       state.get("params") or (persisted.get("params") if persisted else None),
        "completed_at": (persisted.get("completed_at") if persisted else None),
        # Just the fields needed to drive the progress bar + status text.
        "progress": {
            "current_step": snap.get("current_step"),
            "total_steps":  snap.get("total_steps"),
            "cycle":        snap.get("cycle"),
            "current_ts":   snap.get("current_ts"),
            "start_ts":     snap.get("start_ts"),
            "message":      snap.get("message"),
            "error":        snap.get("error"),
            "skipped_symbols":      snap.get("skipped_symbols"),
            "tail_truncated_hours": snap.get("tail_truncated_hours"),
            "tail_bottleneck":      snap.get("tail_bottleneck"),
        },
    })


@bp.get("/api/backtest/snapshot")
def bt_snapshot():
    """Full backtest snapshot (history + timeseries + positions + KPIs).

    Heavy endpoint, called only when the run finishes or the user explicitly
    refreshes. Replaces the snapshot body that used to ship in every
    /api/backtest/status poll.
    """
    state, persisted = _resolve_bt_state()
    if state.get("running") or state.get("snapshot"):
        return jsonify({
            "running":  state.get("running", False),
            "snapshot": state.get("snapshot"),
            "params":   state.get("params"),
        })
    if persisted and persisted.get("snapshot"):
        return jsonify({
            "running":      False,
            "snapshot":     persisted.get("snapshot"),
            "params":       persisted.get("params"),
            "completed_at": persisted.get("completed_at"),
        })
    return jsonify({"running": False, "snapshot": None, "params": None})


@bp.post("/api/backtest/start")
def bt_start():
    global _bt_state, _bt_stop_event
    if _IS_SERVERLESS:
        return jsonify({"error": _SERVERLESS_MSG}), 503
    with _bt_lock:
        if _bt_state["running"]:
            return jsonify({"error": "Backtest déjà en cours"}), 409
        body       = request.json or {}
        cfg        = load_config()
        raw_syms   = body.get("symbols", ",".join(cfg.get("watchlist", [])))
        symbols    = [s.strip().upper() for s in raw_syms.split(",") if s.strip()]
        start_date = body.get("start_date") or None
        days       = max(1, int(body.get("days", 30)))
        budget     = float(body.get("budget", cfg.get("budget", 1000)))
        buy_thr    = int(body.get("buy_threshold", 8))
        top_n      = max(1, int(body.get("top_n", 3)))
        trend_confirm_h = max(0.0, float(body.get("trend_confirm_hours", 24)))
        min_hold_h      = max(0.0, float(body.get("min_hold_hours", 12)))
        rebuy_cd_h      = max(0.0, float(body.get("rebuy_cooldown_hours", 0)))
        risk       = max(1, min(int(body.get("risk_level", cfg.get("risk_level", 3))), 10))
        sell_cd    = max(0, int(body.get("sell_cooldown_cycles", cfg.get("sell_cooldown_cycles", 3))))
        llm_mode   = bool(body.get("llm_mode", False))
        llm_every  = max(1, int(body.get("llm_every_n_candles", 4)))
        decide_every_n = max(1, int(body.get("decide_every_n_candles", 4)))
        _bt_stop_event = threading.Event()
        # Stash the launch params so the frontend's "Paramètres" tab can
        # hydrate from /api/backtest/status (survives page reloads as long
        # as the server process lives).
        launch_params = {
            "symbols":           ",".join(symbols),
            "days":              days,
            "start_date":        start_date,
            "budget":            budget,
            "stop_loss_pct":     float(body.get("stop_loss_pct",     cfg.get("stop_loss_pct", 10))),
            "trailing_stop_pct": float(body.get("trailing_stop_pct", cfg.get("trailing_stop_pct", 5))),
            "risk_level":        risk,
            "buy_threshold":     buy_thr,
            "top_n":             top_n,
            "trend_confirm_hours":  trend_confirm_h,
            "min_hold_hours":       min_hold_h,
            "rebuy_cooldown_hours": rebuy_cd_h,
            "decide_every_n_candles": decide_every_n,
        }
        _bt_state = {"running": True, "loading": True, "snapshot": None,
                     "params": launch_params}

    def _run():
        global _bt_state
        try:
            def on_step(snap):
                with _bt_lock:
                    _bt_state["loading"]  = snap.get("loading", False)
                    _bt_state["snapshot"] = snap

            stop_loss_pct     = float(body.get("stop_loss_pct",     cfg.get("stop_loss_pct", 10)))
            trailing_stop_pct = float(body.get("trailing_stop_pct", cfg.get("trailing_stop_pct", 5)))
            result = bt_engine.run_live(
                symbols=symbols, start_date=start_date, days=days, budget=budget,
                stop_loss_pct=stop_loss_pct,
                trailing_stop_pct=trailing_stop_pct,
                risk_level=risk,
                buy_threshold=buy_thr, top_n=top_n,
                trend_confirm_hours=trend_confirm_h,
                min_hold_hours=min_hold_h,
                rebuy_cooldown_hours=rebuy_cd_h,
                sell_cooldown_cycles=sell_cd,
                decide_every_n_candles=decide_every_n,
                enable_regime_stance=bool(body.get("enable_regime_stance", True)),
                llm_mode=llm_mode, llm_every_n_candles=llm_every,
                on_step=on_step, stop_event=_bt_stop_event,
            )
            with _bt_lock:
                _bt_state = {"running": False, "loading": False,
                             "snapshot": result, "params": launch_params}
            # Persist only successful, non-stopped runs. Stopped/errored runs
            # would replace a previously-good cached result with a partial one.
            if isinstance(result, dict) and not result.get("error"):
                _persist_last_backtest(result, launch_params)
        except Exception as exc:
            with _bt_lock:
                _bt_state = {"running": False, "loading": False,
                             "snapshot": {"error": str(exc)}}

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "symbols": symbols, "budget": budget,
                    "llm_mode": llm_mode, "llm_every_n_candles": llm_every})


@bp.post("/api/backtest/stop")
def bt_stop():
    _bt_stop_event.set()
    return jsonify({"ok": True})


# ── Grid search ──────────────────────────────────────────────────────────────

_grid_lock  = threading.Lock()
_grid_state: dict = {"running": False, "results": [], "progress": 0, "total": 0}
_grid_stop  = threading.Event()


@bp.get("/api/backtest/grid/status")
def grid_status():
    with _grid_lock:
        return jsonify(dict(_grid_state))


@bp.post("/api/backtest/grid/start")
def grid_start():
    global _grid_stop
    if _IS_SERVERLESS:
        return jsonify({"error": _SERVERLESS_MSG}), 503
    with _grid_lock:
        if _grid_state["running"]:
            return jsonify({"error": "Grid search déjà en cours"}), 409

    body = request.json or {}
    cfg  = load_config()
    raw_syms   = body.get("symbols", ",".join(cfg.get("watchlist", [])))
    symbols    = [s.strip().upper() for s in raw_syms.split(",") if s.strip()]
    start_date = body.get("start_date") or None
    days       = max(1, int(body.get("days", 30)))
    budget     = float(body.get("budget", cfg.get("budget", 1000)))

    # Parameter ranges to sweep
    risk_levels = body.get("risk_levels", [3, 5, 7])
    stop_losses = body.get("stop_losses", [5, 10, 15])
    trailing_stops = body.get("trailing_stops", [3, 5, 10])

    combos = list(itertools.product(risk_levels, stop_losses, trailing_stops))

    with _grid_lock:
        _grid_state.update(running=True, results=[], progress=0, total=len(combos))
    _grid_stop = threading.Event()

    def _run():
        results = []
        for i, (risk, sl, ts) in enumerate(combos):
            if _grid_stop.is_set():
                break
            try:
                result = bt_engine.run_live(
                    symbols=symbols, start_date=start_date, days=days, budget=budget,
                    stop_loss_pct=sl, trailing_stop_pct=ts,
                    risk_level=risk, buy_threshold=8, top_n=3,
                    sell_cooldown_cycles=3,
                    llm_mode=False, on_step=None, stop_event=None,
                )
                results.append({
                    "risk_level": risk, "stop_loss": sl, "trailing_stop": ts,
                    "pnl": result.get("pnl", 0),
                    "pnl_pct": result.get("pnl_pct", 0),
                    "trades": result.get("trades", 0),
                    "win_rate": result.get("win_rate"),
                    "alpha": result.get("alpha"),
                    "total_value": result.get("total_value", budget),
                })
            except Exception as exc:
                log.warning("Grid combo risk=%d sl=%d ts=%d failed: %s", risk, sl, ts, exc)
                results.append({
                    "risk_level": risk, "stop_loss": sl, "trailing_stop": ts,
                    "pnl": 0, "error": str(exc),
                })
            with _grid_lock:
                _grid_state["progress"] = i + 1
                _grid_state["results"]  = sorted(results, key=lambda r: r.get("pnl", 0), reverse=True)

        with _grid_lock:
            _grid_state["running"]  = False
            _grid_state["results"]  = sorted(results, key=lambda r: r.get("pnl", 0), reverse=True)

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "combinations": len(combos)})


@bp.post("/api/backtest/grid/stop")
def grid_stop():
    _grid_stop.set()
    return jsonify({"ok": True})
