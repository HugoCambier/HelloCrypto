"""Backtest routes."""
from __future__ import annotations

import itertools
import json
import logging
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request

from .. import backtest as bt_engine
from ..api import load_config

bp  = Blueprint("backtest", __name__)
log = logging.getLogger(__name__)

_bt_lock       = threading.Lock()
_bt_stop_event = threading.Event()
_bt_state: dict = {"running": False, "loading": False, "snapshot": None}
_bt_speed: dict = {"value": 10.0}

# DB key holding the last completed backtest (snapshot + params + completed_at).
# Lets the cockpit + chart survive a Vercel instance recycle: the final run
# is reloaded from the agent_state table when in-memory _bt_state is empty.
_LAST_BACKTEST_KEY = "last_backtest_state"


def _persist_last_backtest(snapshot: dict, params: dict | None) -> None:
    """Save the final backtest snapshot to the DB so a page reload after a
    Vercel cold-start still shows the previous run's results."""
    try:
        from db.store import set_state
        set_state(_LAST_BACKTEST_KEY, json.dumps({
            "snapshot":     snapshot,
            "params":       params,
            "completed_at": datetime.utcnow().isoformat(),
        }))
    except Exception:
        log.warning("Could not persist last backtest snapshot", exc_info=True)


def _load_last_backtest() -> dict | None:
    """Inverse of _persist_last_backtest; returns None on miss."""
    try:
        from db.store import get_state
        raw = get_state(_LAST_BACKTEST_KEY)
        if not raw:
            return None
        return json.loads(raw) if isinstance(raw, str) else raw
    except Exception:
        return None


@bp.get("/api/backtest/status")
def bt_status():
    with _bt_lock:
        state = dict(_bt_state)
    # If there's something live (running, or in-memory snapshot from this
    # process), return that. Otherwise, hydrate from the persisted last
    # completed backtest so the user keeps seeing their previous results
    # even across Vercel instance recycles.
    if state.get("running") or state.get("snapshot"):
        return jsonify(state)
    persisted = _load_last_backtest()
    if persisted and persisted.get("snapshot"):
        return jsonify({
            "running":      False,
            "loading":      False,
            "snapshot":     persisted.get("snapshot"),
            "params":       persisted.get("params"),
            "completed_at": persisted.get("completed_at"),
        })
    return jsonify(state)


@bp.post("/api/backtest/start")
def bt_start():
    global _bt_state, _bt_stop_event
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
        speed      = max(1.0, min(500.0, float(body.get("speed", 10.0))))
        llm_mode   = bool(body.get("llm_mode", False))
        llm_every  = max(1, int(body.get("llm_every_n_candles", 4)))
        decide_every_n = max(1, int(body.get("decide_every_n_candles", 4)))
        _bt_speed["value"] = speed
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
            "speed":             speed,
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
                on_step=on_step, stop_event=_bt_stop_event, speed_ref=_bt_speed,
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


@bp.post("/api/backtest/speed")
def bt_speed_update():
    body  = request.json or {}
    speed = max(1.0, min(500.0, float(body.get("speed", 10.0))))
    _bt_speed["value"] = speed
    # Keep the params snapshot in sync so the Paramètres tab reflects the
    # actually-applied speed, which the user can tweak live mid-run.
    with _bt_lock:
        if isinstance(_bt_state.get("params"), dict):
            _bt_state["params"]["speed"] = speed
    return jsonify({"speed": speed})


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
                    speed_ref={"value": 500},
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
