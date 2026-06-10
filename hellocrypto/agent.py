"""Autonomous trading agent — main loop.

LLM call gating
---------------
Claude/Gemini is only called when BOTH conditions are met:
1. ``llm_cooldown_seconds`` have elapsed since the last LLM call.
2. At least one watched asset has moved by ``price_change_threshold_pct`` or more.

Stop-loss and trailing stop are always evaluated every cycle.
"""

from __future__ import annotations

import logging
import time

from dotenv import load_dotenv

from . import strategy
from .api import (
    compute_scores,
    format_market_data_compact,
    get_balance,
    get_btc_dominance,
    get_enriched_market_data,
    get_fear_and_greed,
    get_open_positions,
    get_ticker,
    load_config,
    load_history,
    market_buy,
    market_sell,
    save_trade,
)
from .deciders import regime_decision
from .eval.behavior import section_for_cycle as _behavior_section
from .eval.capture import capture_snapshots as _capture_snapshots
from .eval.playbook import section_for_cycle as _playbook_section
from .llm import call as llm_call
from .llm import last_usage as llm_last_usage
from .prompts import DECISION_SCHEMA, SYSTEM, build_analysis
from .trading import check_stops as _trading_check_stops
from .trading import compute_position_size

load_dotenv()

log = logging.getLogger(__name__)

# Keys persisted in DB between Cloud Run Job invocations
_STATE_KEY = "agent_real"


# ── Config helpers ───────────────────────────────────────────────────────────

def _load_cfg() -> dict:
    return load_config()


def _load_state() -> dict:
    try:
        from db.store import get_state
        return get_state(_STATE_KEY) or {}
    except ImportError:
        return {}


def _save_state(state: dict) -> None:
    try:
        from db.store import set_state
        set_state(_STATE_KEY, state)
    except ImportError:
        pass


# ── Shared helpers ───────────────────────────────────────────────────────────

def _fetch_market_data(watchlist: list[str], cycle_sec: int) -> dict:
    data = get_enriched_market_data(watchlist, cycle_seconds=cycle_sec)
    for sym in watchlist:
        if sym not in data:
            log.warning("Données indisponibles pour %s", sym)
    return data


def _prices_from_data(data: dict) -> dict:
    return {sym: d["price"] for sym, d in data.items()}


def _max_price_change(current: dict, reference: dict) -> float:
    if not reference:
        return 100.0
    changes = [
        abs(current[s] - reference[s]) / reference[s]
        for s in current if s in reference and reference[s] > 0
    ]
    return max(changes) if changes else 0.0


def _check_stops(positions: dict, prices: dict, peak_prices: dict,
                 stop_loss: float, trail_stop: float):
    """Return stop signals, resolving missing prices via live ticker."""
    enriched_prices = {
        sym: prices.get(sym) or get_ticker(sym)
        for sym in positions
    }
    return _trading_check_stops(positions, enriched_prices, peak_prices, stop_loss, trail_stop)


def _performance_report(prices: dict, positions: dict, cash: float,
                        initial_total_value: float) -> str:
    history = load_history()
    portfolio_val = sum(
        p["qty"] * prices.get(sym, p["avg_price"]) for sym, p in positions.items()
    )
    total = cash + portfolio_val
    pnl = total - initial_total_value
    try:
        from db.store import sum_fees
        total_fees = sum_fees(mode="real")
    except Exception:
        total_fees = sum(t.get("fee", 0) for t in history)
    lines = [
        "═══ RAPPORT DE PERFORMANCE ═══",
        f"Valeur initiale: ${initial_total_value:.2f}",
        f"Valeur totale  : ${total:.2f}",
        f"Cash USDC      : ${cash:.2f}",
        f"Portefeuille   : ${portfolio_val:.2f}",
        f"PnL            : {pnl:+.2f} USDC ({pnl / initial_total_value * 100:+.2f}%)"
        if initial_total_value else "",
        f"Frais cumulés  : ${total_fees:.4f} USDC",
        f"Transactions   : {len(history)}",
        "───────────────────────────────",
    ]
    for sym, p in positions.items():
        cur = prices.get(sym, p["avg_price"])
        pnl_pos = (cur - p["avg_price"]) / p["avg_price"] * 100
        lines.append(f"  {sym}: {p['qty']:.6f} qty  (PnL {pnl_pos:+.2f}%)")
    return "\n".join(lines)


# ── Core cycle logic ─────────────────────────────────────────────────────────

def _execute_cycle(
    cfg: dict,
    cycle: int,
    last_llm_call: float,
    llm_call_count: int,
    ref_prices: dict,
    recent_decisions: list,
    peak_prices: dict,
    cooldown_map: dict,
    initial_total_value: float = 0.0,
    strat_state: dict | None = None,
) -> dict:
    """Execute one trading cycle (shared by run_one_cycle and run_agent).

    Returns an updated state dict with all mutable fields.
    ``initial_total_value`` is captured once on the first cycle from the real
    Binance portfolio (cash + all open positions) and persisted in state.
    ``strat_state`` carries the deterministic decider's per-session timers
    (entry_ts, bear_since_*, last_sell_ts, portfolio_peak, dd_cooldown_until)
    across cycles. Ignored when decider == "llm".
    """
    strat_state = dict(strat_state or {})
    watchlist = cfg["watchlist"]
    budget = float(cfg["budget"])
    stop_loss = float(cfg["stop_loss_pct"]) / 100
    trail_stop = float(cfg.get("trailing_stop_pct", 5)) / 100
    cycle_sec = int(cfg["cycle_seconds"])
    llm_cooldown = int(cfg.get("llm_cooldown_seconds", 300))
    price_threshold = float(cfg.get("price_change_threshold_pct", 0.5)) / 100
    risk_level = int(cfg.get("risk_level", 3))
    sell_cooldown_cyc = int(cfg.get("sell_cooldown_cycles", 3))

    # Tag every trade / market analysis written this cycle with the active
    # real-mode session id (opened by routes/config.config_set when the
    # runner is armed). Old data with NULL session_id stays as catch-all.
    try:
        from db.store import get_state as _get_state
        _real_sid   = _get_state("active_real_session_id") or None
        _real_sname = _get_state("active_real_session_name") or None
    except Exception:
        _real_sid, _real_sname = None, None

    positions = get_open_positions(watchlist)
    cash = get_balance("USDC")
    market_data_raw = _fetch_market_data(watchlist, cycle_sec)
    prices = _prices_from_data(market_data_raw)

    # ── Macro context (cached 5min) + live snapshot persistence ───────────
    # Captured every cycle (even when LLM is skipped) so the dataset feeding
    # the playbook keeps growing. Snapshot save is best-effort: a DB failure
    # does NOT abort the trading cycle.
    fear_greed    = get_fear_and_greed()
    btc_dominance = get_btc_dominance()
    _capture_snapshots(market_data_raw, fear_greed, btc_dominance, cycle=cycle)

    # ── Capture initial portfolio value on first cycle ────────────────────
    if initial_total_value == 0.0:
        portfolio_val = sum(
            p["qty"] * prices.get(sym, p["avg_price"]) for sym, p in positions.items()
        )
        initial_total_value = cash + portfolio_val
        log.info("Valeur initiale du run: $%.2f (USDC) + $%.2f (crypto) = $%.2f",
                 cash, portfolio_val, initial_total_value)

    log.info("Cash: $%.2f USDC | Positions: %s", cash, list(positions.keys()))

    # ── Update peak prices ────────────────────────────────────────────────
    strategy.update_peak_prices(positions, prices, peak_prices)

    # ── Stop-loss + trailing stop ─────────────────────────────────────────
    for sig in _check_stops(positions, prices, peak_prices, stop_loss, trail_stop):
        _, fee, fee_asset = market_sell(sig.symbol, sig.qty)
        save_trade(
            f"SELL ({sig.kind})", sig.symbol, sig.qty, sig.price,
            f"{sig.kind.replace('-', ' ').title()} déclenché", fee, fee_asset,
            session_id=_real_sid, session_name=_real_sname,
        )
        peak_prices.pop(sig.symbol, None)
        cooldown_map[sig.symbol] = cycle
        del positions[sig.symbol]

    # ── Decider routing ──────────────────────────────────────────────────
    # ``deterministic`` bypasses the LLM (and its gating): the decider has
    # its own cadence (``decide_every_cycles``) and runs every cycle, but
    # only emits actions on its decision windows. Stops above still fire
    # every cron tick regardless of decider.
    decider = (cfg.get("decider") or "llm").lower()
    if decider == "deterministic":
        det_params = {
            k: cfg.get(k) for k in (
                "decide_every_cycles", "top_n", "buy_threshold",
                "trend_confirm_hours", "min_hold_hours", "rebuy_cooldown_hours",
                "enable_regime_stance", "exit_signal", "score_exit_threshold",
                "max_portfolio_dd_pct", "dd_cooldown_days",
            ) if cfg.get(k) is not None
        }
        from datetime import date as _date
        fng_v = (fear_greed or {}).get("value") if fear_greed else None
        decision, strat_state = regime_decision(
            market_raw=market_data_raw, holdings=positions, cash=cash,
            cycle=cycle, now_ts=time.time(),
            risk_level=risk_level, strat_state=strat_state,
            params=det_params, fng_value=fng_v,
            as_of_date=_date.today(),
        )
        sentiment = decision.get("market_sentiment", "")
        summary   = decision.get("summary", "")
        log.info("Decider=det | %s | %s", sentiment, summary)
        recent_decisions = (recent_decisions + [decision])[-3:]

        try:
            from db.store import save_market_analysis as _db_analysis
            _db_analysis(
                sentiment=sentiment, summary=summary,
                analyses=decision.get("actions", []), mode="real",
                cycle=cycle, session_id=_real_sid, usage=None, reasoning=None,
            )
        except Exception:
            pass

        # Sells first (free cash), then buys at the decider's pre-sized allocations.
        for action in decision.get("actions", []):
            if action.get("type") != "sell":
                continue
            sym = action.get("symbol")
            if sym not in positions:
                continue
            qty = action.get("qty", positions[sym]["qty"])
            price = prices.get(sym) or get_ticker(sym)
            _, fee, fee_asset = market_sell(sym, qty)
            save_trade("SELL", sym, qty, price, action.get("reason", ""),
                       fee, fee_asset, session_id=_real_sid, session_name=_real_sname)
            peak_prices.pop(sym, None)
            cooldown_map[sym] = cycle
            cash += qty * price  # local estimate; next cycle re-fetches real balance
            del positions[sym]
            log.info("SELL %.6f %s @ $%.4f — %s", qty, sym, price, action.get("reason", ""))

        for action in decision.get("actions", []):
            if action.get("type") != "buy":
                continue
            sym = action.get("symbol")
            amount = float(action.get("usdc_amount") or 0)
            if amount > cash:
                amount = cash
            if amount < 10:
                continue
            _, fee, fee_asset = market_buy(sym, amount)
            price = prices.get(sym) or get_ticker(sym)
            save_trade("BUY", sym, amount, price, action.get("reason", ""),
                       fee, fee_asset, session_id=_real_sid, session_name=_real_sname)
            peak_prices[sym] = price
            cash -= amount
            log.info("BUY  $%.2f %s @ $%.4f — %s", amount, sym, price, action.get("reason", ""))

        if cycle % 10 == 0:
            report = _performance_report(prices, positions, cash, initial_total_value)
            log.info("\n%s", report)

        return {
            "cycle":               cycle,
            "last_llm_call":       last_llm_call,
            "llm_call_count":      llm_call_count,
            "ref_prices":          ref_prices,
            "recent_decisions":    recent_decisions,
            "peak_prices":         peak_prices,
            "cooldown_map":        cooldown_map,
            "initial_total_value": initial_total_value,
            "strat_state":         strat_state,
        }

    # ── LLM gating ────────────────────────────────────────────────────────
    now = time.time()
    cooldown_ok = (now - last_llm_call) >= llm_cooldown
    delta = _max_price_change(prices, ref_prices)
    price_change_ok = delta >= price_threshold

    if not cooldown_ok:
        log.info("Skip LLM — cooldown: %.0fs restants", max(0, llm_cooldown - (now - last_llm_call)))
    elif not price_change_ok:
        log.info("Skip LLM — Δmax %.2f%% < seuil %.1f%%", delta * 100, price_threshold * 100)
    else:
        # fear_greed + btc_dominance already fetched above for snapshot capture
        scores = compute_scores(market_data_raw)
        market_data = format_market_data_compact(market_data_raw, watchlist, scores)
        playbook_section = _playbook_section(fear_greed, market_data_raw, scores=scores)
        behavior_section = _behavior_section(fear_greed, market_data_raw)

        # Regime-adaptive stance (opt-in via config flag).
        stance = None
        if cfg.get("enable_regime_stance", False):
            from .eval.playbook import stance_for_cycle
            stance = stance_for_cycle(
                fear_greed, market_data_raw, btc_dominance,
                float(cfg.get("min_confidence", 0.5) or 0.0),
            )

        decision = llm_call(
            prompt=build_analysis(
                market_data, positions, cash, budget, risk_level,
                recent_decisions, fear_greed, btc_dominance, scores,
                prices=prices, peak_prices=peak_prices,
                cooldown_map=cooldown_map, cycle=cycle,
                playbook_section=playbook_section,
                behavior_section=behavior_section,
                regime_overlay=stance["overlay"] if stance else None,
            ),
            system=SYSTEM,
            config={**cfg, "llm": {**cfg.get("llm", {}), "schema": DECISION_SCHEMA}},
        )

        last_llm_call = time.time()
        ref_prices = dict(prices)
        llm_call_count += 1
        recent_decisions = (recent_decisions + [decision])[-3:]

        log.info("LLM #%d | Sentiment: %s | %s",
                 llm_call_count, decision["market_sentiment"], decision["summary"])

        try:
            from db.store import save_market_analysis as _db_analysis
            _db_analysis(
                sentiment=decision.get("market_sentiment", ""),
                summary=decision.get("summary", ""),
                analyses=decision.get("actions", []),
                mode="real",
                cycle=cycle,
                session_id=_real_sid,
                usage=llm_last_usage(),
                reasoning=decision.get("reasoning"),
            )
        except Exception:
            pass

        min_conf = float(cfg.get("min_confidence", 0.5) or 0.0)
        # Fetch confidence calibration from the cached behavior report. Same
        # bayesian shrinkage as in simulation — pass-through when no data.
        from .eval.behavior import _cached_behavior, calibrate_confidence
        _bh = _cached_behavior() or {}
        _calibration = _bh.get("confidence_calibration") if cfg.get("enable_confidence_calibration", True) else None

        # Regime-aware threshold (opt-in, off by default per overfitting concerns).
        if cfg.get("enable_regime_aware_thresholds", False):
            from .eval.playbook import _cached_playbook, current_regime, regime_aware_min_confidence
            _pb = _cached_playbook()
            btc_trend_1d = market_data_raw.get("BTCUSDC", {}).get("trend_1d") if "BTCUSDC" in market_data_raw else None
            _regime = current_regime(fear_greed, btc_trend_1d)
            adjusted = regime_aware_min_confidence(_pb, _regime, min_conf)
            if abs(adjusted - min_conf) > 0.001:
                log.info("Regime %s → min_confidence %.2f → %.2f", _regime, min_conf, adjusted)
            min_conf = adjusted

        # Regime stance supersedes the above when enabled: confidence gate +
        # cash floor (kept in cash, computed from total portfolio value).
        cash_floor_usd = 0.0
        if stance is not None:
            min_conf = stance["min_confidence"]
            positions_val = sum(p["qty"] * (prices.get(s) or p["avg_price"]) for s, p in positions.items())
            cash_floor_usd = (cash + positions_val) * stance["cash_floor_pct"] / 100.0
            log.info("Regime stance %s → min_conf %.2f, cash floor %.0f%% ($%.2f)",
                     stance["label"], min_conf, stance["cash_floor_pct"], cash_floor_usd)

        for action in decision.get("actions", []):
            atype = action.get("type", "")
            sym = action.get("symbol", "")
            if not atype or not sym:
                continue
            horizon = action.get("horizon", "").upper() if atype == "buy" else ""
            reason  = strategy.format_buy_reason(action) if atype == "buy" else action.get("reason", "")

            # Phase E: gate par confidence — applique uniquement quand le
            # modèle a renvoyé une confidence. Sinon, comportement legacy.
            raw_conf = action.get("confidence")
            if raw_conf is not None and _calibration is not None:
                calibrated = calibrate_confidence(atype, float(raw_conf), _calibration)
                if abs(calibrated - float(raw_conf)) > 0.01:
                    log.info("Calibrate %s %s: %.2f → %.2f",
                             atype.upper(), sym, float(raw_conf), calibrated)
                conf = calibrated
            else:
                conf = raw_conf
            if conf is not None and atype != "hold" and float(conf) < min_conf:
                log.info("Skip %s %s — confidence %.2f < %.2f",
                         atype.upper(), sym, float(conf), min_conf)
                continue

            if atype == "buy" and cash > 10:
                if strategy.in_cooldown(sym, cycle, cooldown_map, sell_cooldown_cyc):
                    log.info("COOLDOWN %s — %d cycles restants",
                             sym, sell_cooldown_cyc - (cycle - cooldown_map[sym]))
                    continue

                rsi = market_data_raw.get(sym, {}).get("rsi14")
                base_amt = float(action.get("usdc_amount") or 0)
                if conf is not None:
                    # Phase E: réduit la taille quand le modèle hésite (×0.5–1.0)
                    base_amt *= max(0.5, min(1.0, float(conf)))
                amount = compute_position_size(base_amt, cash, risk_level, rsi)
                # Regime cash floor: never spend below the reserve.
                if cash_floor_usd > 0 and amount > cash - cash_floor_usd:
                    spendable = cash - cash_floor_usd
                    if spendable < 10:
                        log.info("Skip BUY %s — cash floor atteint (réserve $%.2f)", sym, cash_floor_usd)
                        continue
                    log.info("Clamp BUY %s $%.2f → $%.2f (cash floor)", sym, amount, spendable)
                    amount = spendable
                if amount >= 10:
                    _, fee, fee_asset = market_buy(sym, amount)
                    price = prices.get(sym) or get_ticker(sym)
                    save_trade("BUY", sym, amount, price, reason, fee, fee_asset,
                               session_id=_real_sid, session_name=_real_sname)
                    peak_prices[sym] = price
                    cash -= amount
                    log.info("BUY  $%.2f %s @ $%.4f (RSI=%.0f) [%s]",
                             amount, sym, price, rsi or 0, horizon or "?")

            elif atype == "sell" and sym in positions:
                qty = action.get("qty", positions[sym]["qty"])
                price = prices.get(sym) or get_ticker(sym)
                _, fee, fee_asset = market_sell(sym, qty)
                save_trade("SELL", sym, qty, price, reason, fee, fee_asset,
                           session_id=_real_sid, session_name=_real_sname)
                peak_prices.pop(sym, None)
                cooldown_map[sym] = cycle
                log.info("SELL %.6f %s @ $%.4f", qty, sym, price)

            else:
                log.info("HOLD %s — %s", sym, reason)

    if cycle % 10 == 0:
        report = _performance_report(prices, positions, cash, initial_total_value)
        log.info("\n%s", report)

    return {
        "cycle":               cycle,
        "last_llm_call":       last_llm_call,
        "llm_call_count":      llm_call_count,
        "ref_prices":          ref_prices,
        "recent_decisions":    recent_decisions,
        "peak_prices":         peak_prices,
        "cooldown_map":        cooldown_map,
        "initial_total_value": initial_total_value,
        "strat_state":         strat_state,
    }


# ── Entry points ─────────────────────────────────────────────────────────────

def run_one_cycle() -> None:
    """Execute a single trading cycle — designed for Cloud Run Jobs.

    State (peak_prices, cooldown_map, etc.) is persisted in Firestore/SQLite
    so it survives across invocations triggered by Cloud Scheduler.
    """
    cfg = _load_cfg()
    state = _load_state()
    cycle = state.get("cycle", 0) + 1

    # Attach DB log handler for this cycle, scoped to the active real session
    # (if any) so per-session log views can filter by session_id.
    _db_handler = None
    try:
        from db.store import DBLogHandler, get_state
        _real_sid = get_state("active_real_session_id") or None
        _db_handler = DBLogHandler(mode="real", session_id=_real_sid)
        _db_handler.set_cycle(cycle)
        logging.getLogger().addHandler(_db_handler)
    except ImportError:
        pass

    provider = cfg.get("llm", {}).get("provider", "claude")
    model = cfg.get("llm", {}).get("model", "—")
    risk_level = int(cfg.get("risk_level", 3))
    budget = float(cfg["budget"])
    log.info("Cycle #%d | Budget: $%.0f USDC | LLM: %s/%s | Risque: %d/10",
             cycle, budget, provider, model, risk_level)

    try:
        new_state = _execute_cycle(
            cfg=cfg,
            cycle=cycle,
            last_llm_call=state.get("last_llm_call", 0.0),
            llm_call_count=state.get("llm_call_count", 0),
            ref_prices=state.get("ref_prices", {}),
            recent_decisions=state.get("recent_decisions", []),
            peak_prices=state.get("peak_prices", {}),
            cooldown_map=state.get("cooldown_map", {}),
            initial_total_value=state.get("initial_total_value", 0.0),
            strat_state=state.get("strat_state", {}),
        )
        _save_state(new_state)
    except Exception as exc:
        log.error("Erreur cycle #%d: %s", cycle, exc, exc_info=True)
        _save_state({**state, "cycle": cycle})
    finally:
        if _db_handler is not None:
            logging.getLogger().removeHandler(_db_handler)


def run_agent() -> None:
    """Continuous trading loop — designed for VM / local execution."""
    cfg = _load_cfg()
    provider = cfg.get("llm", {}).get("provider", "claude")
    model = cfg.get("llm", {}).get("model", "—")
    budget = float(cfg["budget"])
    stop_loss = float(cfg["stop_loss_pct"]) / 100
    trail_stop = float(cfg.get("trailing_stop_pct", 5)) / 100
    cycle_sec = int(cfg["cycle_seconds"])
    llm_cooldown = int(cfg.get("llm_cooldown_seconds", 300))
    risk_level = int(cfg.get("risk_level", 3))

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[logging.StreamHandler()],
    )

    log.info(
        "Agent démarré | Budget: $%.0f USDC | Stop-loss: %.0f%% "
        "| Trailing: %.0f%% | LLM: %s/%s "
        "| cooldown: %ds | Risque: %d/10",
        budget, stop_loss * 100, trail_stop * 100,
        provider, model, llm_cooldown, risk_level,
    )

    _db_handler = None
    try:
        from db.store import DBLogHandler as _DBH
        _db_handler = _DBH(mode="real")
        logging.getLogger().addHandler(_db_handler)
    except ImportError:
        pass

    state: dict = {
        "cycle":               0,
        "last_llm_call":       0.0,
        "llm_call_count":      0,
        "ref_prices":          {},
        "recent_decisions":    [],
        "peak_prices":         {},
        "cooldown_map":        {},
        "initial_total_value": 0.0,
        "strat_state":         {},
    }

    while True:
        state["cycle"] += 1
        cycle = state["cycle"]
        if _db_handler is not None:
            _db_handler.set_cycle(cycle)
        log.info("═══ Cycle #%d ═══", cycle)

        try:
            state = _execute_cycle(
                cfg=cfg,
                cycle=cycle,
                last_llm_call=state["last_llm_call"],
                llm_call_count=state["llm_call_count"],
                ref_prices=state["ref_prices"],
                recent_decisions=state["recent_decisions"],
                peak_prices=state["peak_prices"],
                cooldown_map=state["cooldown_map"],
                initial_total_value=state["initial_total_value"],
                strat_state=state.get("strat_state", {}),
            )
        except Exception as exc:
            log.error("Erreur cycle #%d: %s", cycle, exc, exc_info=True)

        time.sleep(cycle_sec)


if __name__ == "__main__":
    run_agent()
