"""Paper-trading simulation engine."""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from . import strategy
from .api import (
    compute_scores,
    format_market_data_compact,
    get_btc_dominance,
    get_enriched_market_data,
    get_fear_and_greed,
    load_config,
)
from .eval.behavior import section_for_cycle as _behavior_section
from .eval.capture import capture_snapshots as _capture_snapshots
from .eval.playbook import section_for_cycle as _playbook_section
from .llm import call as llm_call
from .llm import last_usage as llm_last_usage
from .prompts import DECISION_SCHEMA, SYSTEM, build_analysis
from .trading import FEE_RATE as SIM_FEE_RATE
from .trading import paper_buy, paper_sell

log = logging.getLogger(__name__)

SIM_STATE_FILE = Path("data/simulation_state.json")


# ── Persistence helpers ────────────────────────────────────────────────────────

def _state_key(session_id: str | None) -> str:
    """DB key for a session's persisted state. Per-session so independent
    simulations never clobber each other; legacy single key when unspecified."""
    return f"simulation:{session_id}" if session_id else "simulation"


def _state_file(session_id: str | None):
    """JSON fallback path, mirroring the per-session keying."""
    if not session_id:
        return SIM_STATE_FILE
    return SIM_STATE_FILE.with_name(f"{SIM_STATE_FILE.stem}_{session_id}{SIM_STATE_FILE.suffix}")


def _save_state(state: dict, session_id: str | None = None, *,
                update_saved_at: bool = True) -> None:
    # `saved_at` is the gate the cron uses to space DECISION cycles. Stops-only
    # ticks (which fire between full cycles) preserve the prior saved_at so the
    # next cron heartbeat is still gated against the last decision cycle, not
    # against the most recent stops-only tick.
    saved_at = (
        datetime.utcnow().isoformat()
        if update_saved_at
        else (state.get("saved_at") or datetime.utcnow().isoformat())
    )
    try:
        from db.store import set_state
        set_state(_state_key(session_id),
                  {**state, "saved_at": saved_at, "schema_version": 1})
        return
    except ImportError:
        pass
    # JSON fallback
    try:
        path = _state_file(session_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({**state, "saved_at": saved_at}, indent=2))
    except Exception as exc:
        log.warning("[SIM] Impossible de sauvegarder l'état: %s", exc)


def _load_state(session_id: str | None = None) -> dict | None:
    try:
        from db.store import get_state
        data = get_state(_state_key(session_id))
        if data and data.get("schema_version", 1) != 1:
            log.warning("[SIM] Version de schéma incompatible — démarrage propre")
            return None
        return data
    except ImportError:
        pass
    # JSON fallback
    try:
        data = json.loads(_state_file(session_id).read_text())
        if data.get("schema_version", 1) != 1:
            log.warning("[SIM] Version de schéma incompatible — démarrage propre")
            return None
        return data
    except Exception:
        return None


# ── Stops-only tick (between full decision cycles) ───────────────────────────

def tick_stops_only(session_id: str, config: dict | None = None) -> dict:
    """Run stop-monitoring on a session WITHOUT consuming a decision cycle.

    The cron heartbeat (~5 min) fires this between full ``sim.run`` cycles so
    that hard stop-loss and trailing stops keep firing at heartbeat cadence
    regardless of how slow the user has configured the decision cadence
    (``cycle_seconds``). The session's ``saved_at`` is preserved so the cron
    gate still measures elapsed time against the last DECISION cycle.

    Returns a small status dict; mutations are persisted via ``_save_state``.
    """
    saved = _load_state(session_id)
    if not saved:
        return {"action": "stops_skip", "reason": "no_state"}
    holdings = saved.get("holdings", {}) or {}
    if not holdings:
        return {"action": "stops_skip", "reason": "no_positions"}

    cfg = config or load_config()
    stop_loss  = float(cfg["stop_loss_pct"]) / 100
    trail_stop = float(cfg.get("trailing_stop_pct", 5)) / 100
    cycle_sec  = int(cfg.get("cycle_seconds", 300))

    symbols = list(holdings.keys())
    try:
        market_raw = get_enriched_market_data(symbols, cycle_seconds=cycle_sec)
        prices = {sym: d["price"] for sym, d in market_raw.items() if d.get("price")}
    except Exception as exc:
        log.warning("[SIM-STOPS] fetch prix échoué session=%s: %s", session_id, exc)
        return {"action": "stops_error"}
    if not prices:
        return {"action": "stops_skip", "reason": "no_prices"}

    cash         = float(saved.get("cash", 0))
    peak_prices  = saved.get("peak_prices", {}) or {}
    cooldown_map = {k: int(v) for k, v in (saved.get("cooldown_map", {}) or {}).items()}
    history      = saved.get("history", []) or []
    total_fees   = float(saved.get("total_fees", 0))
    cycle        = int(saved.get("cycle", 0))

    strategy.update_peak_prices(holdings, prices, peak_prices)
    recv, fees, stop_trades = strategy.apply_paper_stops(
        holdings, prices, peak_prices, cooldown_map,
        stop_loss, trail_stop, cycle,
    )
    cash       += recv
    total_fees += fees

    if stop_trades:
        session_name = ""
        try:
            from db.store import get_session as _get_sess
            sess = _get_sess(session_id) or {}
            session_name = sess.get("name") or ""
        except Exception:
            pass
        for t in stop_trades:
            history.append(t.to_history())
            try:
                from db.store import save_trade as _db_save
                _db_save(action=t.action, symbol=t.symbol, amount=None, price=t.price,
                         reason=t.reason, fee=t.fee, qty=t.qty, pnl=t.pnl,
                         mode="simulation", session_id=session_id,
                         session_name=session_name)
            except Exception:
                log.warning("[SIM-STOPS] save_trade échoué", exc_info=True)
        log.info("[SIM-STOPS] session=%s firé=%d stops", session_id, len(stop_trades))

    new_state = {
        **saved,
        "cash":         cash,
        "holdings":     holdings,
        "peak_prices":  peak_prices,
        "cooldown_map": cooldown_map,
        "history":      history,
        "total_fees":   total_fees,
    }
    _save_state(new_state, session_id, update_saved_at=False)
    return {"action": "stops_fired" if stop_trades else "stops_ok",
            "fired": len(stop_trades)}


# ── Snapshot builder ───────────────────────────────────────────────────────────

def _snapshot(cycle, cash, holdings, prices, history, total_fees,
              initial_total_value, initial_prices, cycle_sec=60):
    portfolio_val = sum(
        h["qty"] * prices.get(sym, h["avg_price"]) for sym, h in holdings.items()
    )
    total = cash + portfolio_val
    base  = initial_total_value or 0
    pnl   = total - base

    benchmark_pnl = benchmark_pnl_pct = alpha = None
    btc_bh_pnl = btc_bh_pct = None
    if initial_prices and base > 0:
        valid = [(sym, p0) for sym, p0 in initial_prices.items() if p0 and prices.get(sym)]
        if valid:
            weight     = base / len(valid)
            weight_net = weight * (1 - SIM_FEE_RATE)
            bh_value   = sum(weight_net * prices[sym] / p0 for sym, p0 in valid)
            benchmark_pnl     = round(bh_value - base, 2)
            benchmark_pnl_pct = round((bh_value - base) / base * 100, 2)
            alpha             = round(pnl - (bh_value - base), 2)

        btc_sym = next((s for s in initial_prices if "BTC" in s and initial_prices[s] and prices.get(s)), None)
        if btc_sym:
            btc_val    = base * (1 - SIM_FEE_RATE) * prices[btc_sym] / initial_prices[btc_sym]
            btc_bh_pnl = round(btc_val - base, 2)
            btc_bh_pct = round((btc_val - base) / base * 100, 2)

    trades_only = [t for t in history if t["action"] != "ANALYSE"]
    sells_only  = [t for t in trades_only if "SELL" in t["action"] and "stop" not in t["action"]]
    profitable  = [t for t in sells_only if t.get("pnl", 0) > 0]

    return {
        "cycle":           cycle,
        "cash":            round(cash, 2),
        "portfolio_value": round(portfolio_val, 2),
        "total_value":     round(total, 2),
        "budget":          round(base, 2),
        "pnl":             round(pnl, 2),
        "pnl_pct":         round(pnl / base * 100, 2) if base > 0 else 0,
        "total_fees":      round(total_fees, 4),
        "trades":          len(trades_only),
        "buys":            len([t for t in trades_only if t["action"] == "BUY"]),
        "sells":           len(sells_only),
        "stop_losses":     len([t for t in trades_only if "stop" in t["action"]]),
        "win_rate":        round(len(profitable) / len(sells_only) * 100, 1) if sells_only else None,
        "benchmark_pnl":      benchmark_pnl,
        "benchmark_pnl_pct":  benchmark_pnl_pct,
        "alpha":              alpha,
        "btc_bh_pnl":         btc_bh_pnl,
        "btc_bh_pct":         btc_bh_pct,
        "positions": [
            {
                "symbol":        sym,
                "qty":           round(h["qty"], 6),
                "avg_price":     round(h["avg_price"], 4),
                "current_price": prices.get(sym),
                "value":         round(h["qty"] * prices.get(sym, h["avg_price"]), 2),
                "pnl_pct":       round((prices[sym] - h["avg_price"]) / h["avg_price"] * 100, 2)
                                 if prices.get(sym) else 0,
            }
            for sym, h in holdings.items()
        ],
        "cycle_sec": cycle_sec,
        "history":   list(reversed(history)),
    }


# ── Simulation runner ──────────────────────────────────────────────────────────

def run(
    budget: float,
    config: dict | None = None,
    on_cycle: Callable | None = None,
    stop_event: threading.Event | None = None,
    resume: bool = False,
    max_cycles: int | None = None,
    initial_holdings: dict[str, float] | None = None,
    session_id: str | None = None,
    session_name: str | None = None,
    liquidate_at_end: bool = False,
    decider: str = "llm",
) -> dict:
    """Run the paper-trading simulation.

    ``initial_holdings`` is an optional dict ``{symbol: qty}`` used to seed
    the portfolio on a fresh start (ignored when ``resume=True`` and a saved
    state is found).  avg_price is set to the first-cycle market price.
    """
    import time
    import uuid as _uuid
    if not session_id:
        session_id = _uuid.uuid4().hex[:8]
    if not session_name:
        session_name = datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    log.info("[SIM] Session: %s - %s", session_id, session_name)

    cfg                  = config or load_config()
    watchlist            = cfg["watchlist"]
    stop_loss            = float(cfg["stop_loss_pct"]) / 100
    trail_stop           = float(cfg.get("trailing_stop_pct", 5)) / 100
    cycle_sec            = int(cfg.get("cycle_seconds", 60))
    risk_level           = int(cfg.get("risk_level", 3))
    sell_cooldown_cycles = int(cfg.get("sell_cooldown_cycles", 3))

    try:
        from db.store import DBLogHandler as _DBH
        _db_handler = _DBH(mode="simulation", session_id=session_id)
        logging.getLogger().addHandler(_db_handler)
    except ImportError:
        _db_handler = None

    # ── State initialisation (fresh or resumed) ────────────────────────────────
    cash: float          = budget
    holdings: dict       = {}
    history: list        = []
    recent_decisions: list = []
    total_fees: float    = 0.0
    prices: dict         = {}
    initial_prices: dict = {}
    initial_total_value: float = 0.0
    peak_prices: dict    = {}   # sym → highest price seen since entry
    cooldown_map: dict   = {}   # sym → last sell cycle
    cycle: int           = 0
    value_timeseries: list = []  # [{ts, v}] — total_value per cycle
    snap: dict           = {}    # latest computed snapshot (for save state on exit)
    strat_state: dict    = {}    # per-session deterministic-decider state (cadence, etc.)

    # Deterministic-decider params (only used when decider == "deterministic").
    det_params = {
        "decide_every_cycles":  cfg.get("decide_every_cycles"),
        "top_n":                cfg.get("top_n"),
        "buy_threshold":        cfg.get("buy_threshold"),
        "trend_confirm_hours":  cfg.get("trend_confirm_hours"),
        "min_hold_hours":       cfg.get("min_hold_hours"),
        "rebuy_cooldown_hours": cfg.get("rebuy_cooldown_hours"),
        "enable_regime_stance": cfg.get("enable_regime_stance"),
    }

    if resume:
        saved = _load_state(session_id)
        if saved:
            cycle            = saved.get("cycle", 0)
            cash             = saved.get("cash", budget)
            holdings         = saved.get("holdings", {})
            history          = saved.get("history", [])
            recent_decisions = saved.get("recent_decisions", [])
            total_fees       = saved.get("total_fees", 0.0)
            initial_prices   = saved.get("initial_prices", {})
            initial_total_value = saved.get("initial_total_value", 0.0) or 0.0
            peak_prices      = saved.get("peak_prices", {})
            cooldown_map     = {k: int(v) for k, v in saved.get("cooldown_map", {}).items()}
            budget           = saved.get("budget", budget)
            value_timeseries = saved.get("value_timeseries", [])
            strat_state      = saved.get("strat_state", {}) or {}
            log.info("[SIM] Reprise depuis cycle %d — cash $%.2f", cycle, cash)
        else:
            log.info("[SIM] Aucun état sauvegardé — démarrage propre")

    effective_max = max_cycles
    if liquidate_at_end and max_cycles is not None:
        effective_max = max_cycles + 1

    while True:
        if stop_event and stop_event.is_set():
            log.info("[SIM] Arrêtée par l'utilisateur au cycle %d", cycle)
            break
        if effective_max is not None and cycle >= effective_max:
            log.info("[SIM] max_cycles=%d atteint — arrêt", max_cycles)
            break

        cycle += 1
        is_liquidation_cycle = (liquidate_at_end and max_cycles is not None and cycle > max_cycles)
        if _db_handler is not None:
            _db_handler.set_cycle(cycle)

        # ── Fetch enriched market data ─────────────────────────────────────────
        try:
            market_raw = get_enriched_market_data(watchlist, cycle_seconds=cycle_sec)
            prices     = {sym: d["price"] for sym, d in market_raw.items()}
        except Exception as exc:
            log.error("[SIM] Erreur fetch données cycle %d: %s", cycle, exc, exc_info=True)
            prices = {}
            market_raw = {}

        if not prices:
            if stop_event:
                stop_event.wait(timeout=cycle_sec)
            else:
                time.sleep(cycle_sec)
            continue

        if not initial_prices:
            initial_prices = dict(prices)
            # Seed holdings from initial_holdings on first cycle (fresh start only).
            # initial_holdings accepts {sym: qty} (legacy) or {sym: {qty, avg_price}} (preferred).
            if initial_holdings and not holdings:
                for sym, info in initial_holdings.items():
                    if isinstance(info, dict):
                        qty   = float(info.get("qty", 0))
                        entry = float(info.get("avg_price") or 0) or prices.get(sym)
                    else:
                        qty   = float(info)
                        entry = prices.get(sym)
                    if qty > 0 and sym in prices and entry:
                        holdings[sym] = {"qty": qty, "avg_price": entry}
                        peak_prices[sym] = max(prices[sym], entry)
                        log.info("[SIM] Avoir initial: %s qty=%.6f entry=$%.4f (prix actuel: $%.4f)",
                                 sym, qty, entry, prices[sym])
                        # Synthetic BUY entry to record the initial position in history
                        init_amount = round(qty * entry, 2)
                        init_ts = datetime.utcnow().isoformat()
                        history.append({
                            "cycle":     cycle,
                            "timestamp": init_ts,
                            "action":    "BUY (init)",
                            "symbol":    sym,
                            "qty":       qty,
                            "amount":    init_amount,
                            "price":     entry,
                            "fee":       0.0,
                            "reason":    "Initialisation — avoir détenu au démarrage (prix d'entrée Binance)",
                        })
                        try:
                            from db.store import save_trade as _db_save
                            _db_save(
                                action="BUY (init)", symbol=sym, amount=init_amount,
                                price=entry, reason="Initialisation — avoir détenu au démarrage",
                                fee=0.0, qty=qty, pnl=None,
                                mode="simulation", session_id=session_id, session_name=session_name,
                            )
                        except Exception:
                            pass

            # Budget = capital invested at entry prices (USDC cash + crypto at avg_price).
            # This way, the unrealized gain/loss of pre-existing positions counts toward PnL.
            initial_portfolio_val = sum(h["qty"] * h["avg_price"] for sym, h in holdings.items())
            initial_total_value = cash + initial_portfolio_val
            budget = initial_total_value
            log.info("[SIM] Capital initial (prix d'entrée): $%.2f cash + $%.2f actifs = $%.2f budget",
                     cash, initial_portfolio_val, initial_total_value)

            # Persist initial state to sessions table. The route already wrote
            # the user-facing knobs (risk_level, stop_loss_pct, trailing_stop_pct,
            # cycle_seconds, decider, llm, …) at session creation; we merge our
            # runtime-derived fields on top instead of overwriting, otherwise the
            # Paramètres tab loses the originals.
            try:
                from db.store import (
                    get_session as _get_sess,
                )
                from db.store import (
                    upsert_session as _upsert,
                )
                existing = {}
                try:
                    row = _get_sess(session_id) or {}
                    raw = row.get("initial_state")
                    if isinstance(raw, str):
                        existing = json.loads(raw)
                    elif isinstance(raw, dict):
                        existing = raw
                except Exception:
                    existing = {}
                runtime_state = {
                    "budget":              budget,
                    "initial_prices":      initial_prices,
                    "initial_holdings":    {
                        sym: {"qty": h["qty"], "avg_price": h["avg_price"]}
                        for sym, h in holdings.items()
                    },
                    "initial_total_value": initial_total_value,
                    "watchlist":           watchlist,
                }
                _upsert(
                    session_id=session_id,
                    name=session_name,
                    mode="simulation",
                    initial_state={**existing, **runtime_state},
                )
            except Exception:
                pass

        strategy.update_peak_prices(holdings, prices, peak_prices)

        # ── Stop-loss (hard + trailing) ────────────────────────────────────────
        recv, fees, stop_trades = strategy.apply_paper_stops(
            holdings, prices, peak_prices, cooldown_map,
            stop_loss, trail_stop, cycle,
        )
        cash       += recv
        total_fees += fees
        for t in stop_trades:
            history.append(t.to_history())
            try:
                from db.store import save_trade as _db_save
                _db_save(
                    action=t.action, symbol=t.symbol, amount=None, price=t.price,
                    reason=t.reason, fee=t.fee, qty=t.qty, pnl=t.pnl,
                    mode="simulation", session_id=session_id, session_name=session_name,
                )
            except Exception:
                pass

        # ── Liquidation cycle: force-sell everything ─────────────────────────
        if is_liquidation_cycle:
            log.info("[SIM] Cycle de liquidation — vente de toutes les positions")
            for sym in list(holdings.keys()):
                if sym not in prices:
                    continue
                entry  = holdings[sym]["avg_price"]
                qty    = holdings[sym]["qty"]
                result = paper_sell(sym, qty, prices[sym], holdings)
                cash       += result.received
                total_fees += result.fee
                peak_prices.pop(sym, None)
                pnl = round((prices[sym] - entry) * result.qty - result.fee, 4)
                history.append({
                    "cycle":     cycle,
                    "timestamp": datetime.utcnow().isoformat(),
                    "action":    "SELL (liquidation)",
                    "symbol":    sym,
                    "qty":       result.qty,
                    "price":     prices[sym],
                    "pnl":       pnl,
                    "fee":       round(result.fee, 6),
                    "reason":    "Liquidation finale — conversion en USDC",
                })
                try:
                    from db.store import save_trade as _db_save
                    _db_save(
                        action="SELL (liquidation)", symbol=sym, amount=None,
                        price=prices[sym], reason="Liquidation finale — conversion en USDC",
                        fee=result.fee, qty=result.qty, pnl=pnl,
                        mode="simulation", session_id=session_id, session_name=session_name,
                    )
                except Exception:
                    pass
                log.info("[SIM] LIQUIDATION %s: %.6f @ $%.4f → PnL %+.2f", sym, result.qty, prices[sym], pnl)
            snap = _snapshot(cycle, cash, holdings, prices, history, total_fees, initial_total_value, initial_prices, cycle_sec)
            if on_cycle:
                on_cycle(cycle, snap)
            break

        # ── Fetch global market context + persist live snapshot ────────────────
        # Snapshot capture is per-cycle (not gated by LLM cadence) so the
        # playbook training set grows continuously. Best-effort: a DB miss
        # never blocks the simulation.
        fear_greed    = get_fear_and_greed()
        btc_dominance = get_btc_dominance()
        _capture_snapshots(market_raw, fear_greed, btc_dominance,
                           cycle=cycle, session_id=session_id)
        scores        = compute_scores(market_raw)
        playbook_section = _playbook_section(fear_greed, market_raw, scores=scores)
        behavior_section = _behavior_section(fear_greed, market_raw)

        # Regime-adaptive stance (opt-in via config flag).
        stance = None
        if cfg.get("enable_regime_stance", False):
            from .eval.playbook import stance_for_cycle
            stance = stance_for_cycle(
                fear_greed, market_raw, btc_dominance,
                float(cfg.get("min_confidence", 0.0) or 0.0),
            )

        # ── Deterministic decider (approach C) — isolated from the LLM path ───
        if decider == "deterministic":
            from .deciders import regime_decision
            decision, strat_state = regime_decision(
                market_raw=market_raw, holdings=holdings, cash=cash,
                cycle=cycle, now_ts=time.time(),
                risk_level=risk_level, strat_state=strat_state,
                params=det_params,
            )
            sentiment = decision.get("market_sentiment", "—")
            summary   = decision.get("summary", "")
            log.info("[SIM] Cycle %d | déterministe | %s", cycle, summary)
            recent_decisions = (recent_decisions + [decision])[-3:]
            history.append({
                "cycle": cycle, "timestamp": datetime.utcnow().isoformat(),
                "action": "ANALYSE", "sentiment": sentiment, "reason": summary,
                "symbol": "", "qty": None, "amount": None, "price": None,
                "fee": None, "pnl": None,
            })
            try:
                from db.store import save_market_analysis as _db_analysis
                _db_analysis(sentiment=sentiment, summary=summary,
                             analyses=decision.get("actions", []), mode="simulation",
                             cycle=cycle, session_id=session_id, usage=None, reasoning=None)
            except Exception:
                pass

            # Execute directly: sells first (free cash), then equal-weight buys
            # across the new entries — faithful to the validated backtest sizing.
            for a in decision.get("actions", []):
                sym = a.get("symbol")
                if a.get("type") != "sell" or sym not in holdings or sym not in prices:
                    continue
                entry = holdings[sym]["avg_price"]
                res   = paper_sell(sym, holdings[sym]["qty"], prices[sym], holdings)
                cash       += res.received
                total_fees += res.fee
                peak_prices.pop(sym, None)
                pnl = round((prices[sym] - entry) * res.qty - res.fee, 4)
                _t = {"cycle": cycle, "timestamp": datetime.utcnow().isoformat(),
                      "action": "SELL", "symbol": sym, "qty": res.qty,
                      "amount": round(res.received, 2), "price": prices[sym],
                      "fee": round(res.fee, 6), "pnl": pnl, "reason": a.get("reason")}
                history.append(_t)
                try:
                    from db.store import save_trade as _db_save
                    _db_save(action="SELL", symbol=sym, amount=None, price=prices[sym],
                             reason=a.get("reason"), fee=res.fee, qty=res.qty, pnl=pnl,
                             mode="simulation", session_id=session_id, session_name=session_name)
                except Exception:
                    pass

            # Buys: decider already computed risk-aware usdc_amount per action.
            for a in decision.get("actions", []):
                if a.get("type") != "buy" or a.get("symbol") not in prices:
                    continue
                alloc = float(a.get("usdc_amount", 0) or 0)
                if alloc > cash:
                    alloc = cash
                if alloc < 10:
                    continue
                sym = a["symbol"]
                res = paper_buy(sym, alloc, prices[sym], holdings)
                cash       -= alloc
                total_fees += res.fee
                peak_prices[sym] = prices[sym]
                _t = {"cycle": cycle, "timestamp": datetime.utcnow().isoformat(),
                      "action": "BUY", "symbol": sym, "qty": res.qty,
                      "amount": round(alloc, 2), "price": prices[sym],
                      "fee": round(res.fee, 6), "pnl": None, "reason": a.get("reason")}
                history.append(_t)
                try:
                    from db.store import save_trade as _db_save
                    _db_save(action="BUY", symbol=sym, amount=round(alloc, 2),
                             price=prices[sym], reason=a.get("reason"), fee=res.fee,
                             qty=res.qty, pnl=None, mode="simulation",
                             session_id=session_id, session_name=session_name)
                except Exception:
                    pass

            # Shared end-of-cycle: snapshot, persist, emit, wait.
            snap = _snapshot(cycle, cash, holdings, prices, history, total_fees,
                             initial_total_value, initial_prices, cycle_sec)
            value_timeseries.append({"ts": datetime.utcnow().isoformat(), "v": snap["total_value"]})
            if len(value_timeseries) > 1000:
                step = max(1, len(value_timeseries) // 500)
                value_timeseries = value_timeseries[::step] + [value_timeseries[-1]]
            snap["value_timeseries"] = list(value_timeseries)
            if on_cycle:
                on_cycle(cycle, snap)
            _save_state({**snap,
                         "schema_version": 1, "budget": budget, "cycle": cycle, "cash": cash,
                         "holdings": holdings, "history": history, "total_fees": total_fees,
                         "initial_prices": initial_prices, "peak_prices": peak_prices,
                         "cooldown_map": cooldown_map, "recent_decisions": recent_decisions,
                         "initial_total_value": initial_total_value,
                         "value_timeseries": value_timeseries, "strat_state": strat_state,
                         "session_id": session_id, "session_name": session_name,
                         "running": True, "decider": decider,
                         "params": {"risk_level": risk_level, "cycle_seconds": cycle_sec,
                                    "stop_loss_pct": round(stop_loss * 100, 2),
                                    "trailing_stop_pct": round(trail_stop * 100, 2),
                                    "sell_cooldown_cycles": sell_cooldown_cycles}}, session_id)
            if stop_event:
                stop_event.wait(timeout=cycle_sec)
            else:
                time.sleep(cycle_sec)
            continue

        # ── LLM decision ──────────────────────────────────────────────────────
        market_data = format_market_data_compact(market_raw, watchlist, scores)
        try:
            decision = llm_call(
                prompt=build_analysis(
                    market_data, holdings, cash, budget, risk_level,
                    recent_decisions, fear_greed, btc_dominance, scores,
                    prices=prices, peak_prices=peak_prices,
                    cooldown_map=cooldown_map, total_fees=total_fees,
                    cycle=cycle,
                    playbook_section=playbook_section,
                    behavior_section=behavior_section,
                    regime_overlay=stance["overlay"] if stance else None,
                ),
                system=SYSTEM,
                config={**cfg, "llm": {**cfg.get("llm", {}), "schema": DECISION_SCHEMA}},
            )
        except Exception as exc:
            log.error("[SIM] Erreur LLM cycle %d: %s", cycle, exc)
            snap = _snapshot(cycle, cash, holdings, prices, history, total_fees, initial_total_value, initial_prices, cycle_sec)
            if on_cycle:
                on_cycle(cycle, snap)
            _save_state({**snap,
                         "schema_version": 1, "budget": budget, "cycle": cycle, "cash": cash,
                         "holdings": holdings, "history": history, "total_fees": total_fees,
                         "initial_prices": initial_prices, "peak_prices": peak_prices,
                         "cooldown_map": cooldown_map, "recent_decisions": recent_decisions,
                         "initial_total_value": initial_total_value,
                         "session_id": session_id, "session_name": session_name,
                         "params": {"risk_level": risk_level, "cycle_seconds": cycle_sec,
                                    "stop_loss_pct": round(stop_loss * 100, 2),
                                    "trailing_stop_pct": round(trail_stop * 100, 2),
                                    "sell_cooldown_cycles": sell_cooldown_cycles}}, session_id)
            if stop_event:
                stop_event.wait(timeout=cycle_sec)
            else:
                time.sleep(cycle_sec)
            continue

        sentiment = decision.get("market_sentiment", "—")
        summary   = decision.get("summary", "")
        log.info("[SIM] Cycle %d | %s | %s", cycle, sentiment, summary)
        recent_decisions = (recent_decisions + [decision])[-3:]

        history.append({
            "cycle":     cycle,
            "timestamp": datetime.utcnow().isoformat(),
            "action":    "ANALYSE",
            "sentiment": sentiment,
            "reason":    summary,
            "symbol":    "",
            "qty":       None,
            "amount":    None,
            "price":     None,
            "fee":       None,
            "pnl":       None,
        })

        # Save market analysis to DB
        try:
            from db.store import save_market_analysis as _db_analysis
            _db_analysis(
                sentiment=sentiment,
                summary=summary,
                analyses=decision.get("actions", []),
                mode="simulation",
                cycle=cycle,
                session_id=session_id,
                usage=llm_last_usage(),
                reasoning=decision.get("reasoning"),
            )
        except Exception:
            pass

        # ── Execute paper trades ───────────────────────────────────────────────
        min_conf = float(cfg.get("min_confidence", 0.0) or 0.0)
        # Fetch confidence calibration from the cached behavior report (None
        # when no data yet — apply_paper_actions then passes-through unchanged).
        from .eval.behavior import _cached_behavior
        _bh = _cached_behavior() or {}
        calibration = _bh.get("confidence_calibration") if cfg.get("enable_confidence_calibration", True) else None

        # Confidence gate + cash floor. regime_stance (if on) supersedes the
        # older regime_aware_thresholds.
        cash_floor_pct = 0.0
        if cfg.get("enable_regime_aware_thresholds", False):
            from .eval.playbook import _cached_playbook, current_regime, regime_aware_min_confidence
            _pb = _cached_playbook()
            btc_trend_1d = market_raw.get("BTCUSDC", {}).get("trend_1d") if "BTCUSDC" in market_raw else None
            _regime = current_regime(fear_greed, btc_trend_1d)
            min_conf = regime_aware_min_confidence(_pb, _regime, min_conf)
        if stance is not None:
            min_conf = stance["min_confidence"]
            cash_floor_pct = stance["cash_floor_pct"]

        new_cash, fees, action_trades = strategy.apply_paper_actions(
            actions=decision.get("actions", []),
            holdings=holdings, cash=cash, prices=prices,
            peak_prices=peak_prices, cooldown_map=cooldown_map,
            market_raw=market_raw, cycle=cycle,
            risk_level=risk_level,
            sell_cooldown_cycles=sell_cooldown_cycles,
            min_confidence=min_conf,
            confidence_calibration=calibration,
            cash_floor_pct=cash_floor_pct,
        )
        cash = new_cash
        total_fees += fees
        for t in action_trades:
            history.append(t.to_history())
            try:
                from db.store import save_trade as _db_save
                _db_save(
                    action=t.action, symbol=t.symbol,
                    amount=t.amount, price=t.price, reason=t.reason,
                    fee=t.fee, qty=t.qty, pnl=t.pnl,
                    mode="simulation", session_id=session_id, session_name=session_name,
                )
            except Exception:
                pass

        # ── Emit snapshot & persist state ─────────────────────────────────────
        snap = _snapshot(cycle, cash, holdings, prices, history, total_fees, initial_total_value, initial_prices, cycle_sec)
        # Track per-cycle total value so the PnL chart matches the displayed PnL exactly.
        value_timeseries.append({"ts": datetime.utcnow().isoformat(), "v": snap["total_value"]})
        if len(value_timeseries) > 1000:
            step = max(1, len(value_timeseries) // 500)
            value_timeseries = value_timeseries[::step] + [value_timeseries[-1]]
        snap["value_timeseries"] = list(value_timeseries)
        if on_cycle:
            on_cycle(cycle, snap)

        _save_state({**snap,
                     "schema_version": 1, "budget": budget, "cycle": cycle, "cash": cash,
                     "holdings": holdings, "history": history, "total_fees": total_fees,
                     "initial_prices": initial_prices, "peak_prices": peak_prices,
                     "cooldown_map": cooldown_map, "recent_decisions": recent_decisions,
                     "initial_total_value": initial_total_value,
                     "value_timeseries": value_timeseries, "strat_state": strat_state,
                     "session_id": session_id, "session_name": session_name,
                     "running": True, "decider": decider,
                     "params": {"risk_level": risk_level, "cycle_seconds": cycle_sec,
                                "stop_loss_pct": round(stop_loss * 100, 2),
                                "trailing_stop_pct": round(trail_stop * 100, 2),
                                "sell_cooldown_cycles": sell_cooldown_cycles}}, session_id)

        if stop_event:
            stop_event.wait(timeout=cycle_sec)
        else:
            time.sleep(cycle_sec)

    # Mark simulation as stopped in persisted state
    _save_state({**snap,
                 "schema_version": 1, "budget": budget, "cycle": cycle, "cash": cash,
                 "holdings": holdings, "history": history, "total_fees": total_fees,
                 "initial_prices": initial_prices, "peak_prices": peak_prices,
                 "cooldown_map": cooldown_map, "recent_decisions": recent_decisions,
                 "initial_total_value": initial_total_value,
                 "strat_state": strat_state,
                 "session_id": session_id, "session_name": session_name,
                 "running": False, "decider": decider,
                 "params": {"risk_level": risk_level, "cycle_seconds": cycle_sec,
                            "stop_loss_pct": round(stop_loss * 100, 2),
                            "trailing_stop_pct": round(trail_stop * 100, 2),
                            "sell_cooldown_cycles": sell_cooldown_cycles}}, session_id)

    if _db_handler is not None:
        logging.getLogger().removeHandler(_db_handler)

    return _snapshot(cycle, cash, holdings, prices, history, total_fees, initial_total_value, initial_prices, cycle_sec)
