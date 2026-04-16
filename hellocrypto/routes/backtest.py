"""Backtest routes."""
import itertools
import logging
import threading

from flask import Blueprint, jsonify, request

from ..api import load_config
from .. import backtest as bt_engine

bp  = Blueprint("backtest", __name__)
log = logging.getLogger(__name__)

_bt_lock       = threading.Lock()
_bt_stop_event = threading.Event()
_bt_state: dict = {"running": False, "loading": False, "snapshot": None}
_bt_speed: dict = {"value": 10.0}


@bp.get("/api/backtest/status")
def bt_status():
    with _bt_lock:
        return jsonify(dict(_bt_state))


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
        buy_thr    = int(body.get("buy_threshold", 7))
        sell_thr   = int(body.get("sell_threshold", 3))
        risk       = max(1, min(int(body.get("risk_level", cfg.get("risk_level", 3))), 10))
        sell_cd    = max(0, int(body.get("sell_cooldown_cycles", cfg.get("sell_cooldown_cycles", 3))))
        speed      = max(1.0, min(500.0, float(body.get("speed", 10.0))))
        llm_mode   = bool(body.get("llm_mode", False))
        llm_every  = max(1, int(body.get("llm_every_n_candles", 4)))
        _bt_speed["value"] = speed
        _bt_stop_event = threading.Event()
        _bt_state = {"running": True, "loading": True, "snapshot": None}

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
                risk_level=risk, buy_threshold=buy_thr, sell_threshold=sell_thr,
                sell_cooldown_cycles=sell_cd,
                llm_mode=llm_mode, llm_every_n_candles=llm_every,
                on_step=on_step, stop_event=_bt_stop_event, speed_ref=_bt_speed,
            )
            with _bt_lock:
                _bt_state = {"running": False, "loading": False, "snapshot": result}
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
                    risk_level=risk, buy_threshold=7, sell_threshold=3,
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
