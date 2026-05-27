"""Market analysis & admin routes."""
from __future__ import annotations

import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from flask import Blueprint, jsonify, request

from ..api import (
    compute_scores,
    format_market_data,
    get_btc_dominance,
    get_enriched_market_data,
    get_fear_and_greed,
    load_config,
)
from ..llm import call as llm_call
from ..llm import last_usage as llm_last_usage
from ..prompts import SYSTEM_ANALYSIS, build_market_analysis_single
from ..ratelimit import rate_limit

bp  = Blueprint("analysis", __name__)
log = logging.getLogger(__name__)

_analysis_lock  = threading.Lock()
_analysis_state: dict = {"running": False, "result": None, "error": None}


@bp.get("/api/analysis/status")
def analysis_status():
    with _analysis_lock:
        return jsonify(dict(_analysis_state))


@bp.post("/api/analysis/start")
@rate_limit(max_calls=3, per_seconds=300)  # 3 analyses / 5 min — chaque appel = O(milliers de tokens LLM)
def analysis_start():
    """Run market analysis synchronously and persist to DB.

    Background threads don't survive on serverless (Vercel kills the
    function once the HTTP response is sent), so we block until done.
    """
    global _analysis_state
    with _analysis_lock:
        if _analysis_state["running"]:
            return jsonify({"error": "Analyse déjà en cours"}), 409
        _analysis_state = {"running": True, "result": None, "error": None}

    def _run():
        global _analysis_state
        try:
            cfg           = load_config()
            watchlist     = cfg.get("watchlist", []) or []
            log.info("[ANALYSIS] watchlist (%d) : %s", len(watchlist), watchlist)
            market_raw    = get_enriched_market_data(watchlist, cycle_seconds=300)
            log.info("[ANALYSIS] market_raw loaded for %d/%d symbols", len(market_raw), len(watchlist))
            scores        = compute_scores(market_raw)
            fear_greed    = get_fear_and_greed()
            btc_dominance = get_btc_dominance()

            # One LLM call per symbol — batch mode let the model bâcle les
            # scénarios pour tout sauf la première crypto (BTC dans l'exemple
            # du prompt). Per-symbol garantit que chaque actif a son contexte
            # complet et ses 3 scénarios.
            market_lines = format_market_data(market_raw, watchlist).splitlines()
            sym_lines: dict[str, str] = {}
            for line in market_lines:
                for sym in watchlist:
                    if line.startswith(sym):
                        sym_lines[sym] = line
                        break

            call_cfg = {**cfg, "max_tokens": max(int(cfg.get("max_tokens", 1000)), 800)}
            usages: list[dict] = []
            usages_lock = threading.Lock()

            def _analyze_one(sym: str) -> dict:
                if sym not in market_raw:
                    return {
                        "symbol":        sym,
                        "current_price": None,
                        "sentiment":     "neutral",
                        "confidence":    0,
                        "summary":       "Données de marché indisponibles.",
                        "action":        "hold",
                        "action_reason": "Données de marché manquantes.",
                        "scenarios":     [],
                    }
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
                    u = llm_last_usage()
                    if u:
                        with usages_lock:
                            usages.append(u)
                except Exception as exc:
                    log.warning("[ANALYSIS] %s failed: %s", sym, exc)
                    item = {
                        "sentiment":     "neutral",
                        "confidence":    0,
                        "summary":       f"Erreur LLM : {exc}",
                        "action":        "hold",
                        "action_reason": "Analyse indisponible.",
                        "scenarios":     [],
                    }
                item["symbol"]        = sym
                item["current_price"] = market_raw[sym]["price"]
                return item

            max_workers = min(len(watchlist), 5) or 1
            analyses: list[dict] = [None] * len(watchlist)  # type: ignore[list-item]
            with ThreadPoolExecutor(max_workers=max_workers) as ex:
                futures = {ex.submit(_analyze_one, sym): i for i, sym in enumerate(watchlist)}
                for fut in as_completed(futures):
                    analyses[futures[fut]] = fut.result()

            counts = {"bullish": 0, "neutral": 0, "bearish": 0}
            for a in analyses:
                s = str(a.get("sentiment", "neutral")).lower()
                counts[s if s in counts else "neutral"] += 1
            dom = max(counts, key=counts.get)
            global_sentiment = dom if counts[dom] > len(analyses) // 2 else "neutral"
            market_summary = (
                f"{counts['bullish']} bullish · {counts['neutral']} neutral · "
                f"{counts['bearish']} bearish sur {len(analyses)} actifs."
            )

            agg_usage = None
            if usages:
                agg_usage = {
                    "provider": usages[0].get("provider"),
                    "model":    usages[0].get("model"),
                    "in":       sum(u.get("in", 0)    for u in usages),
                    "out":      sum(u.get("out", 0)   for u in usages),
                    "total":    sum(u.get("total", 0) for u in usages),
                    "calls":    len(usages),
                }

            result = {
                "global_sentiment": global_sentiment,
                "market_summary":   market_summary,
                "analyses":         analyses,
                "generated_at":     datetime.utcnow().isoformat(),
            }

            try:
                from db.store import save_market_analysis
                save_market_analysis(
                    sentiment=global_sentiment,
                    summary=market_summary,
                    analyses=analyses,
                    mode="real",
                    usage=agg_usage,
                )
            except Exception:
                log.warning("Impossible de sauvegarder l'analyse en base", exc_info=True)

            with _analysis_lock:
                _analysis_state = {"running": False, "result": result, "error": None}
        except Exception:
            log.exception("Erreur lors de l'analyse de marché")
            with _analysis_lock:
                _analysis_state = {"running": False, "result": None, "error": "Erreur lors de l'analyse"}

    _run()
    with _analysis_lock:
        state = dict(_analysis_state)
    if state.get("error"):
        return jsonify({"ok": False, "error": state["error"]}), 500
    return jsonify({"ok": True, "result": state.get("result")})


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
    except Exception:
        log.exception("Erreur api_analyses")
        return jsonify({"error": "Erreur lors du chargement des analyses"}), 500
