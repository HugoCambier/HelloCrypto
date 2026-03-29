"""Flask web dashboard — real-time logs, performance stats, portfolio & manual trades."""

import json
import logging
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
    save_config,
    load_history,
    market_buy,
    market_sell,
    save_trade,
    get_enriched_market_data,
    compute_scores,
    format_market_data,
    get_fear_and_greed,
    get_btc_dominance,
)
from .prompts import SYSTEM, SYSTEM_ANALYSIS, build_analysis, build_market_analysis, build_market_analysis_single
from .llm import call as llm_call
from . import simulation as sim_engine
from . import backtest   as bt_engine

load_dotenv()
log = logging.getLogger(__name__)

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

@app.get("/api/watchlist")
def api_watchlist():
    cfg = load_config()
    return jsonify({
        "watchlist":             cfg.get("watchlist", []),
        "stop_loss_pct":         float(cfg.get("stop_loss_pct", 10)),
        "trailing_stop_pct":     float(cfg.get("trailing_stop_pct", 5)),
        "budget":                float(cfg.get("budget", 1000)),
        "risk_level":            int(cfg.get("risk_level", 3)),
        "sell_cooldown_cycles":  int(cfg.get("sell_cooldown_cycles", 3)),
    })


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


@app.get("/api/simulation/saved")
def sim_saved():
    state_file = _ROOT / "data" / "simulation_state.json"
    if not state_file.exists():
        return jsonify({"exists": False})
    try:
        data = json.loads(state_file.read_text())
        return jsonify({
            "exists":   True,
            "cycle":    data.get("cycle", 0),
            "cash":     data.get("cash", 0),
            "budget":   data.get("budget", 0),
            "saved_at": data.get("saved_at", ""),
            "pnl":      round(data.get("cash", 0) + sum(
                h["qty"] * h["avg_price"] for h in data.get("holdings", {}).values()
            ) - data.get("budget", 0), 2),
        })
    except Exception:
        return jsonify({"exists": False})


@app.post("/api/simulation/start")
def sim_start():
    global _sim_state, _sim_stop_event
    with _sim_lock:
        if _sim_state["running"]:
            return jsonify({"error": "Simulation déjà en cours"}), 409
        body       = request.json or {}
        cfg        = load_config()
        budget              = float(body.get("budget", cfg.get("budget", 100)))
        risk_level          = max(1, min(int(body.get("risk_level", cfg.get("risk_level", 3))), 10))
        cycle_sec           = max(5, int(body.get("cycle_seconds", cfg.get("cycle_seconds", 60))))
        stop_loss_pct       = float(body.get("stop_loss_pct", cfg.get("stop_loss_pct", 10)))
        trailing_stop_pct   = float(body.get("trailing_stop_pct", cfg.get("trailing_stop_pct", 5)))
        sell_cooldown_cycles = max(0, int(body.get("sell_cooldown_cycles", cfg.get("sell_cooldown_cycles", 3))))
        resume              = bool(body.get("resume", False))
        run_cfg             = {**cfg, "risk_level": risk_level, "cycle_seconds": cycle_sec,
                               "stop_loss_pct": stop_loss_pct, "trailing_stop_pct": trailing_stop_pct,
                               "sell_cooldown_cycles": sell_cooldown_cycles}
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
                resume=resume,
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


# ── Backtest ──────────────────────────────────────────────────────────────────

_bt_lock        = threading.Lock()
_bt_stop_event  = threading.Event()
_bt_state: dict = {"running": False, "loading": False, "snapshot": None}
_bt_speed: dict = {"value": 10.0}


@app.get("/api/backtest/status")
def bt_status():
    with _bt_lock:
        return jsonify(dict(_bt_state))


@app.post("/api/backtest/start")
def bt_start():
    global _bt_state, _bt_stop_event
    with _bt_lock:
        if _bt_state["running"]:
            return jsonify({"error": "Backtest déjà en cours"}), 409
        body       = request.json or {}
        cfg        = load_config()
        raw_syms   = body.get("symbols", ",".join(cfg.get("watchlist", [])))
        symbols    = [s.strip().upper() for s in raw_syms.split(",") if s.strip()]
        start_date = body.get("start_date") or None   # "YYYY-MM-DD" or null
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


@app.post("/api/backtest/stop")
def bt_stop():
    _bt_stop_event.set()
    return jsonify({"ok": True})


@app.post("/api/backtest/speed")
def bt_speed_update():
    body  = request.json or {}
    speed = max(1.0, min(500.0, float(body.get("speed", 10.0))))
    _bt_speed["value"] = speed
    return jsonify({"speed": speed})


# ── Config API ───────────────────────────────────────────────────────────────

_DEFAULT_LLM_MODELS = {
    "claude": ["claude-opus-4-6", "claude-sonnet-4-6", "claude-haiku-4-5-20251001",
               "claude-opus-4-5", "claude-haiku-4-5"],
    "gemini": ["gemini-2.0-flash", "gemini-2.0-flash-lite", "gemini-1.5-pro",
               "gemini-3.1-flash-lite-preview"],
    "ollama": ["llama3.2", "llama3.1", "mistral", "deepseek-r1", "qwen2.5"],
}


def _llm_models() -> dict:
    """Return llm_models from config.json, falling back to defaults."""
    return load_config().get("llm_models", _DEFAULT_LLM_MODELS)


@app.get("/api/config/llm")
def config_llm_get():
    cfg    = load_config()
    models = _llm_models()
    return jsonify({
        "provider":    cfg.get("llm", {}).get("provider", "gemini"),
        "model":       cfg.get("llm", {}).get("model", ""),
        "base_url":    cfg.get("llm", {}).get("base_url", "http://localhost:11434"),
        "temperature": float(cfg.get("llm", {}).get("temperature", 1.0)),
        "max_tokens":  int(cfg.get("max_tokens", 1000)),
        "providers":   list(models.keys()),
        "models":      models,
    })


@app.post("/api/config/llm")
def config_llm_set():
    body     = request.json or {}
    provider = body.get("provider", "").lower().strip()
    model    = body.get("model", "").strip()
    base_url = body.get("base_url", "").strip()
    max_tok  = body.get("max_tokens")
    temp     = body.get("temperature")
    models   = _llm_models()

    if not provider or provider not in models:
        return jsonify({"error": f"Provider invalide. Valeurs: {list(models.keys())}"}), 400
    if not model:
        return jsonify({"error": "model requis"}), 400

    cfg = load_config()
    cfg["llm"] = {"provider": provider, "model": model}
    if base_url and provider == "ollama":
        cfg["llm"]["base_url"] = base_url
    if temp is not None:
        cfg["llm"]["temperature"] = max(0.0, min(2.0, float(temp)))
    if max_tok is not None:
        cfg["max_tokens"] = max(100, int(max_tok))
    save_config(cfg)
    return jsonify({"ok": True, "provider": provider, "model": model})


@app.get("/api/ollama/status")
def ollama_status():
    import urllib.request as _ur
    cfg      = load_config()
    base_url = cfg.get("llm", {}).get("base_url", "http://localhost:11434").rstrip("/")
    try:
        with _ur.urlopen(f"{base_url}/api/tags", timeout=3) as r:
            tags = json.loads(r.read())
        models = [m["name"] for m in tags.get("models", [])]
        return jsonify({"running": True, "models": models})
    except Exception:
        return jsonify({"running": False, "models": []})


@app.post("/api/ollama/start")
def ollama_start():
    """Try to launch Ollama daemon in background (macOS / Linux)."""
    import urllib.request as _ur
    cfg      = load_config()
    base_url = cfg.get("llm", {}).get("base_url", "http://localhost:11434").rstrip("/")
    # Already running?
    try:
        with _ur.urlopen(f"{base_url}/api/tags", timeout=2):
            return jsonify({"ok": True, "already_running": True})
    except Exception:
        pass
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-a", "Ollama"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            subprocess.Popen(["ollama", "serve"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return jsonify({"ok": True, "already_running": False})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500


# ── Market Analysis ───────────────────────────────────────────────────────────

_analysis_lock  = threading.Lock()
_analysis_state: dict = {"running": False, "result": None, "error": None}


@app.get("/api/analysis/status")
def analysis_status():
    with _analysis_lock:
        return jsonify(dict(_analysis_state))


@app.post("/api/analysis/start")
def analysis_start():
    global _analysis_state
    with _analysis_lock:
        if _analysis_state["running"]:
            return jsonify({"error": "Analyse déjà en cours"}), 409
        _analysis_state = {"running": True, "result": None, "error": None}

    def _run():
        global _analysis_state
        try:
            cfg           = load_config()
            watchlist     = cfg.get("watchlist", [])
            market_raw    = get_enriched_market_data(watchlist, cycle_seconds=300)
            scores        = compute_scores(market_raw)
            fear_greed    = get_fear_and_greed()
            btc_dominance = get_btc_dominance()
            provider      = cfg.get("llm", {}).get("provider", "gemini").lower()

            # For local models (ollama): one call per symbol to avoid truncation
            if provider == "ollama":
                market_lines = format_market_data(market_raw, watchlist).splitlines()
                # map symbol -> data line
                sym_lines: dict[str, str] = {}
                for line in market_lines:
                    for sym in watchlist:
                        if line.startswith(sym):
                            sym_lines[sym] = line
                            break
                analyses = []
                # Use higher token budget for each single-symbol call
                call_cfg = {**cfg, "max_tokens": max(int(cfg.get("max_tokens", 1000)), 600)}
                for sym in watchlist:
                    if sym not in market_raw:
                        continue
                    try:
                        item = llm_call(
                            prompt=build_market_analysis_single(
                                sym, sym_lines.get(sym, sym),
                                fear_greed, btc_dominance,
                                scores.get(sym) if scores else None,
                            ),
                            system=SYSTEM_ANALYSIS,
                            config=call_cfg,
                        )
                        item["symbol"]        = sym
                        item["current_price"] = market_raw[sym]["price"]
                        analyses.append(item)
                    except Exception as exc:
                        log.warning("[ANALYSIS] %s failed: %s", sym, exc)
                        analyses.append({
                            "symbol":       sym,
                            "current_price": market_raw[sym]["price"],
                            "sentiment":    "neutral",
                            "confidence":   0,
                            "summary":      f"Erreur LLM : {exc}",
                            "scenarios":    [],
                        })
                result = {
                    "global_sentiment": "neutral",
                    "market_summary":   "Analyse par symbole (mode Ollama).",
                    "analyses":         analyses,
                    "generated_at":     datetime.utcnow().isoformat(),
                }
            else:
                # Cloud models: single call, generous token budget
                market_data = format_market_data(market_raw, watchlist)
                call_cfg    = {**cfg, "max_tokens": max(int(cfg.get("max_tokens", 1000)), 4000)}
                result = llm_call(
                    prompt=build_market_analysis(market_data, fear_greed, btc_dominance, scores),
                    system=SYSTEM_ANALYSIS,
                    config=call_cfg,
                )
                result["generated_at"] = datetime.utcnow().isoformat()
                for item in result.get("analyses", []):
                    sym = item.get("symbol", "")
                    if sym in market_raw:
                        item["current_price"] = market_raw[sym]["price"]

            with _analysis_lock:
                _analysis_state = {"running": False, "result": result, "error": None}
        except Exception as exc:
            with _analysis_lock:
                _analysis_state = {"running": False, "result": None, "error": str(exc)}

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True})


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    (_ROOT / "logs").mkdir(exist_ok=True)
    (_ROOT / "data").mkdir(exist_ok=True)
    print("Dashboard → http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)


if __name__ == "__main__":
    main()
