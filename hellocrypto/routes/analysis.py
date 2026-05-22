"""Market analysis & admin routes."""
from __future__ import annotations

import logging
import threading
from datetime import datetime

from flask import Blueprint, jsonify, request

from ..api import (
    load_config,
    get_enriched_market_data,
    compute_scores,
    format_market_data,
    get_fear_and_greed,
    get_btc_dominance,
)
from ..prompts import SYSTEM_ANALYSIS, build_market_analysis, build_market_analysis_single
from ..llm import call as llm_call

bp  = Blueprint("analysis", __name__)
log = logging.getLogger(__name__)

_analysis_lock  = threading.Lock()
_analysis_state: dict = {"running": False, "result": None, "error": None}


@bp.get("/api/analysis/status")
def analysis_status():
    with _analysis_lock:
        return jsonify(dict(_analysis_state))


@bp.post("/api/analysis/start")
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
            log.info("[ANALYSIS] watchlist (%d) : %s", len(watchlist), watchlist)
            market_raw    = get_enriched_market_data(watchlist, cycle_seconds=300)
            log.info("[ANALYSIS] market_raw loaded for %d/%d symbols", len(market_raw), len(watchlist))
            scores        = compute_scores(market_raw)
            fear_greed    = get_fear_and_greed()
            btc_dominance = get_btc_dominance()
            provider      = cfg.get("llm", {}).get("provider", "gemini").lower()

            if provider == "ollama":
                market_lines = format_market_data(market_raw, watchlist).splitlines()
                sym_lines: dict[str, str] = {}
                for line in market_lines:
                    for sym in watchlist:
                        if line.startswith(sym):
                            sym_lines[sym] = line
                            break
                analyses = []
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
                            "symbol":        sym,
                            "current_price": market_raw[sym]["price"],
                            "sentiment":     "neutral",
                            "confidence":    0,
                            "summary":       f"Erreur LLM : {exc}",
                            "scenarios":     [],
                        })
                result = {
                    "global_sentiment": "neutral",
                    "market_summary":   "Analyse par symbole (mode Ollama).",
                    "analyses":         analyses,
                    "generated_at":     datetime.utcnow().isoformat(),
                }
            else:
                market_data = format_market_data(market_raw, watchlist)
                # ~500 tokens per crypto with full scenarios, min 4000
                needed_tokens = max(4000, 500 * len(watchlist))
                call_cfg = {**cfg, "max_tokens": max(int(cfg.get("max_tokens", 1000)), needed_tokens)}
                log.info("[ANALYSIS] max_tokens=%d for %d cryptos", call_cfg["max_tokens"], len(watchlist))
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

            # Backfill any watchlist symbol the model skipped, so all cryptos appear.
            analyses_out = list(result.get("analyses", []))
            present = {a.get("symbol") for a in analyses_out if a.get("symbol")}
            for sym in watchlist:
                if sym in present:
                    continue
                analyses_out.append({
                    "symbol":        sym,
                    "current_price": market_raw.get(sym, {}).get("price"),
                    "sentiment":     "neutral",
                    "confidence":    0,
                    "summary":       "Analyse indisponible pour cet actif.",
                    "action":        "hold",
                    "action_reason": "Aucune analyse retournée par le modèle.",
                    "scenarios":     [],
                })
            result["analyses"] = analyses_out

            # Persist to DB so GET /api/analyses can find it
            try:
                from db.store import save_market_analysis
                save_market_analysis(
                    sentiment=result.get("global_sentiment") or result.get("sentiment", "neutral"),
                    summary=result.get("market_summary") or result.get("summary", ""),
                    analyses=result.get("analyses", []),
                    mode="real",
                )
            except Exception:
                log.warning("Impossible de sauvegarder l'analyse en base", exc_info=True)

            with _analysis_lock:
                _analysis_state = {"running": False, "result": result, "error": None}
        except Exception as exc:
            log.exception("Erreur lors de l'analyse de marché")
            with _analysis_lock:
                _analysis_state = {"running": False, "result": None, "error": "Erreur lors de l'analyse"}

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True})


@bp.get("/api/analyses")
def api_analyses():
    mode       = request.args.get("mode")
    session_id = request.args.get("session_id")
    limit      = int(request.args.get("limit", 100))
    try:
        from db.store import load_market_analyses
        return jsonify(load_market_analyses(
            mode=mode or None, session_id=session_id or None, limit=limit,
        ))
    except Exception as exc:
        log.exception("Erreur api_analyses")
        return jsonify({"error": "Erreur lors du chargement des analyses"}), 500


@bp.post("/api/admin/clean-logs")
def admin_clean_logs():
    body            = request.json or {}
    older_than_days = int(body.get("older_than_days", 30))
    keep_last       = body.get("keep_last")
    mode            = body.get("mode")
    session_id      = body.get("session_id")
    try:
        from db.store import clean_logs
        deleted = clean_logs(
            older_than_days=older_than_days,
            mode=mode or None,
            session_id=session_id or None,
            keep_last=int(keep_last) if keep_last is not None else None,
        )
        return jsonify({"ok": True, "deleted": deleted})
    except Exception as exc:
        log.exception("Erreur admin_clean_logs")
        return jsonify({"error": "Erreur lors du nettoyage des logs"}), 500
