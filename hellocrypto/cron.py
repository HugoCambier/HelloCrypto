"""Cron tick logic — shared between local runner and Vercel HTTP endpoint.

GitHub Actions runners are on US Azure IPs that Binance blocks (HTTP 451).
Vercel functions pinned to cdg1 (Paris) work fine. So the cron schedule
stays on GitHub Actions, but the actual work runs on Vercel via a curl
to /api/cron/tick.
"""
from __future__ import annotations

import logging
import threading
from datetime import datetime

log = logging.getLogger(__name__)


def tick(stop_event: threading.Event | None = None) -> dict:
    """Execute one cron tick.

    Priority:
      1. Active simulation in DB → run one cycle (time-gated by cycle_seconds)
      2. Else config.enabled and mode=real → run one real agent cycle
      3. Else no-op

    Also runs an idempotent daily log purge (logs >14 days) so the Supabase
    free tier doesn't fill up.
    """
    import db.store as store

    if stop_event is None:
        stop_event = threading.Event()

    _maybe_purge_old_logs()
    _maybe_purge_old_snapshots()
    # Note: playbook / behavior rebuilds are NOT called here. They run on
    # their own daily schedule via /api/cron/learn so a slow rebuild
    # cannot delay a trading decision cycle.

    # Collect a 5-min market snapshot on EVERY tick, regardless of decision
    # cadence (a deterministic run with cycle=4h would otherwise only refresh
    # prices every 4h). The data is stored with interval='5m' so it coexists
    # with the hourly grid; purged after 7 days to keep DB size in check.
    _capture_5min_market_data()

    # Sims and the real runner can be armed simultaneously and are processed
    # on the same fire — they share the heartbeat but each gates itself by
    # its own cycle_seconds (see agent.run_one_cycle / _run_sim_cycle).
    # Failures are isolated per session/runner: one erroring never blocks
    # the others. **Real runs first** because it touches money on Binance —
    # if the cron ever hits a wall clock limit, real must have executed.
    #
    # Source of truth for "is real armed?" is ``active_real_session_id`` in
    # the DB, not ``cfg.enabled`` in config.json. A stale enabled=true in
    # config.json (e.g. after a server restart, before any UI Resume) must
    # NOT trigger trading — the user has to explicitly Resume from the UI,
    # which calls _maybe_toggle_real_session to open the session record.
    real_result: dict | None = None
    active_real_sid = store.get_state("active_real_session_id") or None
    if active_real_sid:
        try:
            from hellocrypto.agent import run_one_cycle
            run_one_cycle()
            real_result = {"action": "real_cycle", "session_id": active_real_sid}
        except Exception as exc:
            log.exception("[REAL] cycle a échoué")
            real_result = {"action": "real_error", "error": str(exc)}

    sim_results: list = []
    active_sims = store.get_state("active_sims") or {}
    if isinstance(active_sims, dict) and active_sims:
        for entry in list(active_sims.values()):
            try:
                sim_results.append(_run_sim_cycle(entry, threading.Event()))
            except Exception as exc:
                log.exception("[SIM] cycle session %s a échoué", entry.get("session_id"))
                sim_results.append({"action": "sim_error", "session_id": entry.get("session_id"),
                                    "error": str(exc)})

    if real_result and sim_results:
        return {"action": "real_and_sim", "real": real_result, "sims": sim_results}
    if real_result:
        return real_result
    if sim_results:
        return {"action": "sim_cycles", "count": len(sim_results), "results": sim_results}
    return {"action": "skip", "reason": "no active real session, no active sims"}


def _maybe_purge_old_logs() -> None:
    """Purge logs older than 14 days, at most once per 24h.

    Cheap (one indexed delete) but skipped on most ticks via a sentinel in
    agent_state. Failures are logged and swallowed — never block the tick.
    """
    import db.store as store
    try:
        last = store.get_state("last_log_purge_at")
        if last:
            elapsed = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds()
            if elapsed < 86400:
                return
        deleted = store.clean_logs(older_than_days=14)
        store.set_state("last_log_purge_at", datetime.utcnow().isoformat())
        if deleted:
            log.info("[CRON] Purge logs >14j: %d lignes supprimées", deleted)
    except Exception:
        log.warning("[CRON] Purge logs échouée", exc_info=True)


def _maybe_purge_old_snapshots() -> None:
    """Purge 5-min snapshots older than 7 days, at most once per 24h.

    Hourly snapshots (interval='1h') are kept indefinitely — only the dense
    5-min stream is trimmed, since it's only useful for short-term context
    (last-week intraday) and accumulates ~3000 rows/day/coin otherwise.
    """
    import db.store as store
    try:
        last = store.get_state("last_snapshot_purge_at")
        if last:
            elapsed = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds()
            if elapsed < 86400:
                return
        from db.snapshots import purge_old_snapshots
        deleted = purge_old_snapshots(retention_days=7, interval="5m")
        store.set_state("last_snapshot_purge_at", datetime.utcnow().isoformat())
        if deleted:
            log.info("[CRON] Purge snapshots 5m >7j: %d lignes supprimées", deleted)
    except Exception:
        log.warning("[CRON] Purge snapshots échouée", exc_info=True)


def _capture_5min_market_data() -> None:
    """Fetch + persist a market snapshot for the configured watchlist.

    Throttled to ~once per 15 min (every 3rd 5-min heartbeat) via a DB
    sentinel: capturing on every tick dominated Vercel Active CPU even with
    no run armed. Rows stay tagged interval='5m' (the dense intraday stream).
    Best-effort: any failure is logged and swallowed.
    """
    import db.store as store
    try:
        last = store.get_state("last_5min_capture_at")
        if last:
            elapsed = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds()
            if elapsed < 840:
                return

        from hellocrypto.api import (
            get_btc_dominance,
            get_enriched_market_data,
            get_fear_and_greed,
            load_config,
        )
        from hellocrypto.eval.capture import capture_snapshots_5min

        cfg = load_config() or {}
        watchlist = cfg.get("watchlist") or []
        if not watchlist:
            return
        # Reuse the 5-min cache window so back-to-back ticks within the same
        # window don't double-fetch.
        market = get_enriched_market_data(watchlist, cycle_seconds=300)
        if not market:
            return
        fng = None
        dom = None
        try:
            fng = get_fear_and_greed()
        except Exception:
            pass
        try:
            dom = get_btc_dominance()
        except Exception:
            pass
        n = capture_snapshots_5min(market, fng, dom)
        store.set_state("last_5min_capture_at", datetime.utcnow().isoformat())
        if n:
            log.info("[CRON] Capture 5min: %d snapshots persistés", n)
    except Exception:
        log.warning("[CRON] Capture 5min échouée", exc_info=True)


def _maybe_rebuild_playbook() -> None:
    """Regenerate the trading playbook from price_snapshots, at most once per 24h.

    Distills 12+ months of OHLCV+regime data into the favored/avoid lists
    consumed by the decision prompt. Runs after the log purge, before any
    trading work — if it fails, the previous playbook stays in DB and is
    used unchanged. Cost: ~5s on 87k rows; fits easily in the Vercel
    function budget.
    """
    import db.store as store
    try:
        last = store.get_state("last_playbook_rebuild_at")
        if last:
            elapsed = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds()
            if elapsed < 86400:
                return
        from hellocrypto.eval.journal import run_full_analysis
        from hellocrypto.eval.playbook import build_playbook, save_playbook
        # source=None pools backfill + live so the playbook reflects the
        # full accumulated dataset (initial backfill + everything captured
        # since by the live agent).
        journal  = run_full_analysis(source=None, min_samples=50)
        playbook = build_playbook(journal)
        save_playbook(playbook)
        store.set_state("last_playbook_rebuild_at", datetime.utcnow().isoformat())
        n_regimes = len(playbook.get("by_regime", {}))
        log.info("[CRON] Playbook rebuilt: %d régimes, %d patterns matched",
                 n_regimes, playbook.get("n_pattern_matches_total", 0))
    except Exception:
        log.warning("[CRON] Rebuild playbook échoué", exc_info=True)


def _maybe_rebuild_behavior() -> None:
    """Refresh the behavior report (agent's past trades vs realised outcomes).

    Lighter than the playbook (only joins ``trades`` + ``price_snapshots``),
    so we rebuild every 6h to keep the agent's track record reasonably fresh
    in the decision prompt without spamming the cron path.
    """
    import db.store as store
    try:
        last = store.get_state("last_behavior_rebuild_at")
        if last:
            elapsed = (datetime.utcnow() - datetime.fromisoformat(last)).total_seconds()
            if elapsed < 6 * 3600:
                return
        from hellocrypto.eval.behavior import compute_behavior, save_behavior
        # Pool simulation + real trades for now (the agent learns from both).
        report = compute_behavior(mode=None)
        save_behavior(report)
        store.set_state("last_behavior_rebuild_at", datetime.utcnow().isoformat())
        n_regimes = len(report.get("by_regime", {}))
        log.info("[CRON] Behavior rebuilt: %d régimes couverts sur %d trades",
                 n_regimes, report.get("n_trades", 0))
    except Exception:
        log.warning("[CRON] Rebuild behavior échoué", exc_info=True)


def _run_sim_cycle(active_sim: dict, stop_event: threading.Event) -> dict:
    import db.store as store
    from hellocrypto import simulation as sim
    from hellocrypto.api import load_config

    params         = active_sim.get("params") or {}
    cycle_seconds  = int(params.get("cycle_seconds") or 300)
    sid            = active_sim.get("session_id", "")
    sname          = active_sim.get("session_name", "")
    decider        = active_sim.get("decider", "llm")
    is_first_cycle = not active_sim.get("started")

    # Time-gate: between full decision cycles we still run a stops-only tick
    # so stop-loss / trailing fire at the cron heartbeat (~5 min) regardless
    # of the decision cadence. Only the LLM/deterministic decider is
    # throttled by cycle_seconds. `saved_at` is preserved by stops-only ticks
    # so the gate keeps measuring elapsed time against the last DECISION
    # cycle. Tolerance (60s) absorbs the cycle's own runtime, so e.g. a
    # 5-min cycle finishing at 16:00:20 does not block the 16:05:00 fire.
    GATE_TOLERANCE_SEC = 60
    cfg     = load_config()
    budget  = float(params.get("budget") or cfg.get("budget", 100))
    run_cfg = {**cfg, **{k: v for k, v in params.items() if v is not None}}

    # Gate check : lite projection of {session_id, saved_at, cycle} only.
    # Was loading the full state (200+ KB with history + timeseries) just to
    # read saved_at — every heartbeat × every active sim. Now ~100 bytes.
    meta = sim._load_state_meta(sid) or {}
    if meta.get("session_id") == sid and meta.get("saved_at"):
        try:
            elapsed = (datetime.utcnow() - datetime.fromisoformat(meta["saved_at"])).total_seconds()
            if elapsed < cycle_seconds - GATE_TOLERANCE_SEC:
                res = sim.tick_stops_only(sid, run_cfg)
                log.info("[SIM] Stops-only tick session=%s elapsed=%.0fs/%ds → %s (fired=%d)",
                         sid, elapsed, cycle_seconds, res.get("action"), res.get("fired", 0))
                return {"action": "sim_stops_only", "session_id": sid,
                        "elapsed": elapsed, "cycle_seconds": cycle_seconds, **res}
        except Exception:
            log.warning("[SIM] Could not parse saved_at, running anyway", exc_info=True)
    initial_holdings = active_sim.get("initial_holdings") if is_first_cycle else None

    # sim.run's max_cycles is an ABSOLUTE counter (compared against the
    # persisted cycle), not invocation-local. To run exactly one more cycle,
    # we pass (current_cycle + 1).
    current_cycle = int(meta.get("cycle", 0)) if meta.get("session_id") == sid else 0
    target_max = current_cycle + 1

    log.info("[SIM] Running 1 cycle | session=%s | first=%s | from_cycle=%d → max=%d | cycle_seconds=%ds",
             sid, is_first_cycle, current_cycle, target_max, cycle_seconds)

    # Short-circuit the post-cycle wait inside sim.run by setting stop_event
    # from the on_cycle callback. Without this, sim.run idles for cycle_seconds
    # before exiting once max_cycles is reached.
    def _short_circuit(_c: int, _s: dict) -> None:
        stop_event.set()

    sim.run(
        budget,
        config=run_cfg,
        on_cycle=_short_circuit,
        stop_event=stop_event,
        resume=not is_first_cycle,
        max_cycles=target_max,
        initial_holdings=initial_holdings,
        session_id=sid,
        session_name=sname,
        liquidate_at_end=False,
        decider=decider,
    )

    if is_first_cycle:
        active_sims = store.get_state("active_sims") or {}
        if sid in active_sims:
            active_sims[sid] = {**active_sims[sid], "started": True}
            store.set_state("active_sims", active_sims)

    return {"action": "sim_cycle", "session_id": sid, "cycle": target_max}
