"""Flask web dashboard — real-time logs, performance stats, portfolio & manual trades."""

import json
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, Response, jsonify, render_template, request

from .api import (
    get_balance,
    get_open_positions,
    get_ticker,
    load_config,
    load_history,
    market_buy,
    market_sell,
    save_trade,
)
from . import simulation as sim_engine

load_dotenv()

# Resolve paths relative to the project root (two levels up from this file)
_ROOT = Path(__file__).parent.parent

app = Flask(__name__, template_folder=str(_ROOT / "templates"))

_LOG_FILE      = _ROOT / "logs" / "agent.log"
_agent_process = None

PERIODS: dict[str, timedelta] = {
    "1h":  timedelta(hours=1),
    "6h":  timedelta(hours=6),
    "24h": timedelta(hours=24),
    "3j":  timedelta(days=3),
    "7j":  timedelta(days=7),
    "30j": timedelta(days=30),
    "all": timedelta(days=9999),
}


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return render_template("index.html")


# ── SSE: real-time logs ───────────────────────────────────────────────────────

@app.get("/api/logs/stream")
def stream_logs():
    def generate():
        _LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LOG_FILE.touch()
        with _LOG_FILE.open("r", encoding="utf-8") as fh:
            for line in fh.readlines()[-200:]:          # historical tail
                yield f"data: {json.dumps(line.rstrip())}\n\n"
            while True:                                   # live tail
                line = fh.readline()
                if line:
                    yield f"data: {json.dumps(line.rstrip())}\n\n"
                else:
                    time.sleep(0.4)

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ── Performance ───────────────────────────────────────────────────────────────

@app.get("/api/performance")
def api_performance():
    period  = request.args.get("period", "24h")
    history = load_history()
    config  = load_config()
    cutoff  = datetime.utcnow() - PERIODS.get(period, timedelta(hours=24))

    filtered    = [t for t in history if datetime.fromisoformat(t["timestamp"]) >= cutoff]
    buys        = [t for t in filtered if t["action"] == "BUY"]
    sells       = [t for t in filtered if "SELL" in t["action"] and "stop" not in t["action"]]
    stop_losses = [t for t in filtered if "stop-loss" in t["action"]]

    invested  = sum(t["amount"] for t in buys)
    recovered = sum(t["amount"] * t["price"] for t in sells + stop_losses)
    fees      = sum(t.get("fee", 0) for t in filtered)

    return jsonify({
        "period":      period,
        "trades":      len(filtered),
        "buys":        len(buys),
        "sells":       len(sells),
        "stop_losses": len(stop_losses),
        "invested":    round(invested, 2),
        "recovered":   round(recovered, 2),
        "fees":        round(fees, 4),
        "net":         round(recovered - invested - fees, 2),
        "history":     list(reversed(filtered[-100:])),
        "budget":      config.get("budget", 100),
    })


# ── Portfolio ─────────────────────────────────────────────────────────────────

@app.get("/api/portfolio")
def api_portfolio():
    try:
        config    = load_config()
        watchlist = config.get("watchlist", [])
        positions = get_open_positions(watchlist)
        cash      = get_balance("USDC")

        prices = {}
        for sym in watchlist:
            try:
                prices[sym] = get_ticker(sym)
            except Exception:
                prices[sym] = None

        portfolio_val = sum(
            p["qty"] * prices[sym]
            for sym, p in positions.items()
            if prices.get(sym)
        )
        total      = cash + portfolio_val
        budget     = float(config.get("budget", 100))
        gain       = total - budget
        total_fees = sum(t.get("fee", 0) for t in load_history())

        return jsonify({
            "cash":          round(cash, 2),
            "portfolio_val": round(portfolio_val, 2),
            "total":         round(total, 2),
            "budget":        budget,
            "gain":          round(gain, 2),
            "gain_pct":      round(gain / budget * 100, 2) if budget else 0,
            "total_fees":    round(total_fees, 4),
            "positions": [
                {
                    "symbol":        sym,
                    "qty":           p["qty"],
                    "avg_price":     round(p["avg_price"], 4),
                    "current_price": prices.get(sym),
                    "value":         round(p["qty"] * prices[sym], 2) if prices.get(sym) else None,
                    "pnl_pct":       round((prices[sym] - p["avg_price"]) / p["avg_price"] * 100, 2)
                                     if prices.get(sym) else 0,
                }
                for sym, p in positions.items()
            ],
            "market": [
                {"symbol": sym, "price": prices[sym]}
                for sym in watchlist
                if prices[sym] is not None
            ],
        })
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Manual trade actions ──────────────────────────────────────────────────────

@app.post("/api/trade/buy")
def api_buy():
    body   = request.json or {}
    symbol = body.get("symbol", "").strip().upper()
    amount = float(body.get("amount", 0))
    if not symbol or amount <= 0:
        return jsonify({"error": "symbol et amount requis"}), 400
    try:
        _, fee, fee_asset = market_buy(symbol, amount)
        price = get_ticker(symbol)
        save_trade("BUY", symbol, amount, price, "Ordre manuel — dashboard", fee, fee_asset)
        return jsonify({"ok": True, "price": price, "fee": fee, "fee_asset": fee_asset})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


@app.post("/api/trade/sell")
def api_sell():
    body   = request.json or {}
    symbol = body.get("symbol", "").strip().upper()
    qty    = float(body.get("qty", 0))
    if not symbol or qty <= 0:
        return jsonify({"error": "symbol et qty requis"}), 400
    try:
        _, fee, fee_asset = market_sell(symbol, qty)
        price = get_ticker(symbol)
        save_trade("SELL", symbol, qty, price, "Ordre manuel — dashboard", fee, fee_asset)
        return jsonify({"ok": True, "price": price, "fee": fee, "fee_asset": fee_asset})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 500


# ── Agent lifecycle ───────────────────────────────────────────────────────────

@app.get("/api/agent/status")
def agent_status():
    global _agent_process
    running = _agent_process is not None and _agent_process.poll() is None
    return jsonify({"running": running, "pid": _agent_process.pid if running else None})


@app.post("/api/agent/start")
def agent_start():
    global _agent_process
    if _agent_process and _agent_process.poll() is None:
        return jsonify({"status": "already_running", "pid": _agent_process.pid})
    # Use the same Python interpreter (respects Poetry venv)
    _agent_process = subprocess.Popen(
        [sys.executable, "-m", "hellocrypto.agent"],
        cwd=str(_ROOT),
    )
    return jsonify({"status": "started", "pid": _agent_process.pid})


@app.post("/api/agent/stop")
def agent_stop():
    global _agent_process
    if _agent_process and _agent_process.poll() is None:
        _agent_process.terminate()
        _agent_process.wait(timeout=5)
        return jsonify({"status": "stopped"})
    return jsonify({"status": "not_running"})


# ── Simulation ───────────────────────────────────────────────────────────────

_sim_lock       = threading.Lock()
_sim_stop_event = threading.Event()
_sim_state: dict = {"running": False, "snapshot": None}


@app.get("/api/simulation/status")
def sim_status():
    with _sim_lock:
        return jsonify(dict(_sim_state))


@app.post("/api/simulation/start")
def sim_start():
    global _sim_state, _sim_stop_event
    with _sim_lock:
        if _sim_state["running"]:
            return jsonify({"error": "Simulation déjà en cours"}), 409
        body       = request.json or {}
        cfg        = load_config()
        budget      = float(body.get("budget", cfg.get("budget", 100)))
        risk_level  = max(1, min(int(body.get("risk_level", cfg.get("risk_level", 3))), 10))
        cycle_sec   = max(5, int(body.get("cycle_seconds", cfg.get("cycle_seconds", 60))))
        run_cfg     = {**cfg, "risk_level": risk_level, "cycle_seconds": cycle_sec}
        _sim_stop_event = threading.Event()
        _sim_state = {
            "running":  True,
            "snapshot": {"cycle": 0, "pnl": 0, "trades": 0, "history": [], "positions": []},
        }

    def _run():
        global _sim_state
        try:
            def on_cycle(cycle, snapshot):
                with _sim_lock:
                    _sim_state["snapshot"] = snapshot

            result = sim_engine.run(
                budget,
                config=run_cfg,
                on_cycle=on_cycle,
                stop_event=_sim_stop_event,
            )
            with _sim_lock:
                _sim_state = {"running": False, "snapshot": result}
        except Exception as exc:
            with _sim_lock:
                _sim_state = {"running": False, "snapshot": {"error": str(exc)}}

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "budget": budget, "risk_level": risk_level, "cycle_seconds": cycle_sec})


@app.post("/api/simulation/stop")
def sim_stop():
    global _sim_stop_event
    _sim_stop_event.set()
    return jsonify({"ok": True})


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    (_ROOT / "logs").mkdir(exist_ok=True)
    (_ROOT / "data").mkdir(exist_ok=True)
    print("Dashboard → http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)


if __name__ == "__main__":
    main()
