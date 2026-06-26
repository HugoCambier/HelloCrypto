"""Simulation routes + SimState thread-safe container."""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime

from flask import Blueprint, jsonify, request

from .. import simulation as sim_engine
from ..api import load_config

log = Blueprint("simulation", __name__)
bp  = log  # alias — exported as `bp`
log = logging.getLogger(__name__)


# ── SimState ──────────────────────────────────────────────────────────────────

class SimState:
    """Thread-safe container for the running simulation state."""

    def __init__(self) -> None:
        self._lock            = threading.Lock()
        self.running          = False
        self.session_id       = ""
        self.session_name     = ""
        self.cycle_seconds    = 60
        self.cycle_started_at: str | None = None
        self.snapshot: dict   = {}
        self.error: str | None = None

    def start(self, session_id: str, session_name: str, cycle_seconds: int) -> None:
        with self._lock:
            self.running          = True
            self.session_id       = session_id
            self.session_name     = session_name
            self.cycle_seconds    = cycle_seconds
            self.cycle_started_at = None
            self.snapshot         = {"cycle": 0, "pnl": 0, "trades": 0, "history": [], "positions": []}
            self.error            = None

    def update_cycle(self, snapshot: dict) -> None:
        with self._lock:
            self.snapshot         = snapshot
            self.cycle_started_at = datetime.utcnow().isoformat()

    def finish(self, snapshot: dict) -> None:
        with self._lock:
            self.running  = False
            self.snapshot = snapshot
            self.error    = None

    def fail(self, exc: Exception) -> None:
        with self._lock:
            self.running = False
            self.error   = str(exc)

    def stop(self) -> None:
        with self._lock:
            self.running = False

    def to_dict(self) -> dict:
        with self._lock:
            # Drop the history array from the live-status payload (best/worst are
            # pre-aggregated in the snapshot); mirrors the serverless projection
            # so both deployments expose the same lightweight shape.
            snap = copy.deepcopy(self.snapshot)
            snap.pop("history", None)
            return {
                "running":          self.running,
                "session_id":       self.session_id,
                "session_name":     self.session_name,
                "cycle_seconds":    self.cycle_seconds,
                "cycle_started_at": self.cycle_started_at,
                "snapshot":         snap,
                "error":            self.error,
            }


class SimRegistry:
    """Holds every concurrently-running simulation session, keyed by id.

    Each entry is fully independent — its own SimState, stop_event and thread —
    so starting/stopping one session never affects the others.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, dict] = {}

    def add(self, session_id: str, state: SimState, stop_event: threading.Event) -> None:
        with self._lock:
            self._sessions[session_id] = {"state": state, "stop_event": stop_event}

    def get(self, session_id: str) -> dict | None:
        with self._lock:
            return self._sessions.get(session_id)

    def remove(self, session_id: str) -> None:
        with self._lock:
            self._sessions.pop(session_id, None)

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._sessions)

    def states(self) -> list[dict]:
        with self._lock:
            return [e["state"].to_dict() for e in self._sessions.values()]

    def running_ids(self) -> list[str]:
        with self._lock:
            return [sid for sid, e in self._sessions.items() if e["state"].running]


_sim_registry = SimRegistry()

# Vercel can't run long-lived threads (function dies after each HTTP response).
# When detected, the dashboard persists a flag in DB; the GitHub Actions runner
# picks it up and executes cycles. See [[vercel-serverless-sim]].
_IS_SERVERLESS  = bool(os.getenv("VERCEL"))


def _read_active_sims() -> dict:
    """All serverless-active sessions, keyed by session_id."""
    try:
        from db.store import get_state
        v = get_state("active_sims")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


# Collapses the 15s status poll (× open tabs) within a warm serverless
# container — each call otherwise does 1 + N DB reads. Status only advances on
# the 5-min cron cycle, so an 8s TTL is lossless; start/stop bust it for an
# instant UI reflection.
_STATUS_CACHE: dict = {"t": 0.0, "data": None}
_STATUS_TTL = 8.0


def _bust_status_cache() -> None:
    _STATUS_CACHE["data"] = None


def _write_active_sims(d: dict) -> None:
    try:
        from db.store import set_state
        set_state("active_sims", d)
        _bust_status_cache()
    except Exception:
        log.warning("Impossible d'écrire active_sims", exc_info=True)


def _upsert_active_sim(entry: dict) -> None:
    d = _read_active_sims()
    d[entry["session_id"]] = entry
    _write_active_sims(d)


def _remove_active_sim(session_id: str) -> None:
    d = _read_active_sims()
    if d.pop(session_id, None) is not None:
        _write_active_sims(d)


def _compute_next_cycle_at(active_sim: dict, snap_data: dict) -> str | None:
    """When the next cron-driven cycle should fire, aligned to the GH Actions
    5-min UTC clock boundary. Lets the UI countdown be honest about cron timing.
    """
    import math
    from datetime import datetime, timedelta

    params = active_sim.get("params") or {}
    cycle_seconds = int(params.get("cycle_seconds") or 300)

    last_saved = (snap_data.get("saved_at")
                  if snap_data.get("session_id") == active_sim.get("session_id") else None)
    if last_saved:
        try:
            target = datetime.fromisoformat(last_saved) + timedelta(seconds=cycle_seconds)
        except Exception:
            target = datetime.utcnow()
    else:
        # No cycle has run yet for this session → next fire is the next 5-min boundary
        target = datetime.utcnow()

    epoch_target  = target.timestamp()
    aligned_epoch = math.ceil(epoch_target / 300) * 300
    return datetime.utcfromtimestamp(aligned_epoch).isoformat()


def _serverless_status_list() -> list[dict]:
    """One status dict per serverless-active session (cron model)."""
    out: list[dict] = []
    for sid, active in _read_active_sims().items():
        snap_data = sim_engine._load_state_status(sid) or {}
        params = active.get("params", {})
        if snap_data.get("session_id") != sid:
            snap_data = {
                "cycle": 0, "pnl": 0, "trades": 0, "history": [], "positions": [],
                "holdings": {}, "session_id": sid,
                "session_name": active.get("session_name", ""),
                "budget": params.get("budget", 100),
            }
        out.append({
            "running":          True,
            "session_id":       sid,
            "session_name":     active.get("session_name", ""),
            "decider":          active.get("decider", "llm"),
            "cycle_seconds":    int(params.get("cycle_seconds") or 60),
            "cycle_started_at": snap_data.get("saved_at"),
            "next_cycle_at":    _compute_next_cycle_at(active, snap_data),
            "snapshot":         snap_data,
            "error":            None,
        })
    return out

# ── Auto-resume on cold start ─────────────────────────────────────────────────

def _spawn_session(sid: str, sname: str, budget: float, run_cfg: dict, *,
                   resume: bool, initial_holdings: dict | None, decider: str) -> None:
    """Register a session and start its own independent background thread."""
    cycle_sec  = int(run_cfg.get("cycle_seconds", 60))
    state      = SimState()
    stop_event = threading.Event()
    state.start(sid, sname, cycle_sec)
    _sim_registry.add(sid, state, stop_event)

    def _run():
        try:
            result = sim_engine.run(
                budget,
                config=run_cfg,
                on_cycle=lambda _c, snap: state.update_cycle(snap),
                stop_event=stop_event,
                resume=resume,
                initial_holdings=initial_holdings if not resume else None,
                session_id=sid,
                session_name=sname,
                decider=decider,
            )
            state.finish(result)
        except Exception as exc:
            log.exception("[SIM] Crash thread session %s", sid)
            state.fail(exc)

    threading.Thread(target=_run, daemon=True).start()


def _try_auto_resume() -> None:
    """Restart every session that was running when the container was killed."""
    cfg = load_config()
    try:
        from db.store import list_simulation_sessions_v2
        sessions = list_simulation_sessions_v2()
    except Exception:
        sessions = []

    for s in sessions:
        sid = s.get("session_id") or s.get("id")
        if not sid or _sim_registry.get(sid):
            continue
        saved = sim_engine._load_state(sid)
        if not saved or not saved.get("running"):
            continue
        sname  = saved.get("session_name", "auto-resume")
        budget = saved.get("budget", float(cfg.get("budget", 100)))
        params = saved.get("params") or {}
        run_cfg = {**cfg, **{k: v for k, v in params.items() if v is not None}}
        log.info("[SIM] Auto-resume session %s depuis cycle %d", sid, saved.get("cycle", 0))
        _spawn_session(sid, sname, budget, run_cfg, resume=True,
                       initial_holdings=None, decider=saved.get("decider", "llm"))


_auto_resumed = False


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.get("/api/simulation/keepalive")
def sim_keepalive():
    """Called by Cloud Scheduler to keep the container alive during a run."""
    global _auto_resumed
    if not _auto_resumed:
        _auto_resumed = True
        _try_auto_resume()
    return jsonify({"running": bool(_sim_registry.running_ids())})


@bp.get("/api/simulation/status")
def sim_status():
    """Status of all sessions. Shape: {"sessions": [<status dict>, ...]}.

    On serverless there's at most one active session (the cron model); we still
    wrap it in the list so the UI has a single shape to consume.
    """
    if _IS_SERVERLESS:
        now = time.time()
        if _STATUS_CACHE["data"] is None or (now - _STATUS_CACHE["t"]) >= _STATUS_TTL:
            _STATUS_CACHE["data"] = _serverless_status_list()
            _STATUS_CACHE["t"] = now
        return jsonify({"sessions": _STATUS_CACHE["data"]})
    global _auto_resumed
    if not _auto_resumed:
        _auto_resumed = True
        _try_auto_resume()
    return jsonify({"sessions": _sim_registry.states()})


@bp.get("/api/simulation/saved")
def sim_saved():
    try:
        data = sim_engine._load_state()
    except Exception:
        data = None
    if not data or not data.get("cycle"):
        return jsonify({"exists": False})
    try:
        holdings = data.get("holdings", {})
        cash     = float(data.get("cash", 0))
        budget   = float(data.get("budget", 0))
        portfolio_val = sum(
            float(h.get("qty", 0)) * float(h.get("avg_price", 0))
            for h in holdings.values()
        )
        params = data.get("params") or {}
        return jsonify({
            "exists":       True,
            "cycle":        data.get("cycle", 0),
            "cash":         round(cash, 6),
            "budget":       round(budget, 2),
            "saved_at":     data.get("saved_at", ""),
            "session_id":   data.get("session_id", ""),
            "session_name": data.get("session_name", ""),
            "holdings": {
                sym: round(float(h.get("qty", 0)), 8)
                for sym, h in holdings.items()
                if float(h.get("qty", 0)) > 0
            },
            "pnl": round(cash + portfolio_val - budget, 2),
            "params": {
                "risk_level":           params.get("risk_level"),
                "cycle_seconds":        params.get("cycle_seconds"),
                "stop_loss_pct":        params.get("stop_loss_pct"),
                "trailing_stop_pct":    params.get("trailing_stop_pct"),
                "sell_cooldown_cycles": params.get("sell_cooldown_cycles"),
            },
        })
    except Exception:
        log.warning("Erreur de lecture de l'état de simulation sauvegardé", exc_info=True)
        return jsonify({"exists": False})


@bp.post("/api/simulation/start")
def sim_start():
    # Multiple independent sessions may run concurrently. On serverless the
    # cron model advances every active session per fire (see Phase 1c).
    body                 = request.json or {}
    cfg                  = load_config()
    budget               = float(body.get("budget", cfg.get("budget", 100)))
    risk_level           = max(1, min(int(body.get("risk_level", cfg.get("risk_level", 5))), 10))
    cycle_sec            = max(5, int(body.get("cycle_seconds", cfg.get("cycle_seconds", 60))))
    # On serverless (GitHub Actions cron, min 5 min interval), enforce 300s floor
    if _IS_SERVERLESS:
        cycle_sec = max(cycle_sec, 300)
    stop_loss_pct        = float(body.get("stop_loss_pct", cfg.get("stop_loss_pct", 10)))
    trailing_stop_pct    = float(body.get("trailing_stop_pct", cfg.get("trailing_stop_pct", 5)))
    sell_cooldown_cycles = max(0, int(body.get("sell_cooldown_cycles", cfg.get("sell_cooldown_cycles", 3))))
    decider              = "deterministic" if body.get("decider") == "deterministic" else "llm"
    # Deterministic-decider params — only meaningful when decider == "deterministic".
    det_keys = ("decide_every_cycles", "top_n", "buy_threshold",
                "trend_confirm_hours", "min_hold_hours", "rebuy_cooldown_hours")
    det_params = {k: body.get(k) for k in det_keys if body.get(k) is not None}
    resume               = bool(body.get("resume", False))
    from_binance         = bool(body.get("from_binance", False))
    raw_holdings = body.get("initial_holdings") or {}
    # Accept either {sym: qty} (legacy) or {sym: {qty, avg_price}} (preferred)
    initial_holdings: dict = {}
    for sym, info in raw_holdings.items():
        if isinstance(info, dict):
            qty = float(info.get("qty", 0))
            avg = float(info.get("avg_price", 0)) or None
            if qty > 0:
                initial_holdings[sym] = {"qty": qty, "avg_price": avg}
        else:
            qty = float(info)
            if qty > 0:
                initial_holdings[sym] = {"qty": qty, "avg_price": None}

    # Fetch Binance holdings + entry prices + USDC balance only when explicitly requested
    if not resume and not initial_holdings and from_binance:
        try:
            from hellocrypto.api import get_balance as _get_bal
            from hellocrypto.api import get_open_positions as _get_pos
            watchlist = cfg.get("watchlist", [])
            fetched = _get_pos(watchlist)
            initial_holdings = {
                sym: {"qty": info["qty"], "avg_price": info.get("avg_price")}
                for sym, info in fetched.items() if info["qty"] > 0
            }
            if "budget" not in body and initial_holdings:
                usdc = _get_bal("USDC")
                if usdc > 0:
                    budget = usdc
            if initial_holdings:
                log.info("[SIM] Avoirs Binance auto-fetchés (avec prix d'entrée): %s + $%.2f USDC",
                         {k: f"{v['qty']:.6f}@${v['avg_price']:.4f}" for k, v in initial_holdings.items()}, budget)
        except Exception as exc:
            log.warning("[SIM] from_binance demandé mais auto-fetch a échoué: %s", exc)

    # Resume targets an existing session by id; a fresh run gets a new id.
    session_id   = (body.get("session_id") if resume else None) or uuid.uuid4().hex[:8]
    session_name = body.get("session_name") or datetime.utcnow().strftime("%Y-%m-%d %H:%M")

    # If resume requested but no saved state for that session → downgrade.
    resume_failed = False
    if resume and not sim_engine._load_state(session_id):
        resume = False
        resume_failed = True

    if _sim_registry.get(session_id) and (_sim_registry.get(session_id) or {}).get("state").running:
        return jsonify({"error": "Cette session tourne déjà"}), 409

    # Per-run watchlist: the modal lets the user pick a subset for this run
    # specifically — we keep it isolated from the global cfg.watchlist so
    # other parts of the app (Marché page, future runs) aren't affected.
    body_watchlist = body.get("watchlist")
    if isinstance(body_watchlist, list) and body_watchlist:
        run_watchlist = [str(s).upper() for s in body_watchlist if s]
    else:
        run_watchlist = cfg.get("watchlist", [])

    run_cfg      = {**cfg, "risk_level": risk_level, "cycle_seconds": cycle_sec,
                    "stop_loss_pct": stop_loss_pct, "trailing_stop_pct": trailing_stop_pct,
                    "sell_cooldown_cycles": sell_cooldown_cycles,
                    "watchlist": run_watchlist, "decider": decider, **det_params}

    try:
        from db.store import upsert_session
        upsert_session(session_id, session_name, mode="simulation",
                       initial_state={
                           "budget":               budget,
                           "watchlist":            run_cfg.get("watchlist", []),
                           "initial_holdings":     initial_holdings,
                           "risk_level":           risk_level,
                           "cycle_seconds":        cycle_sec,
                           "stop_loss_pct":        stop_loss_pct,
                           "trailing_stop_pct":    trailing_stop_pct,
                           "sell_cooldown_cycles": sell_cooldown_cycles,
                           "decider":              decider,
                           "llm":                  cfg.get("llm"),
                           **det_params,
                       })
    except Exception:
        log.warning("Impossible de sauvegarder la session dans la base", exc_info=True)

    if _IS_SERVERLESS:
        # Register the session — the cron runner advances every active session
        # per fire, each gated by its own cycle_seconds (Phase 1c).
        _upsert_active_sim({
            "session_id":       session_id,
            "session_name":     session_name,
            "decider":          decider,
            "params": {
                "budget":               budget,
                "risk_level":           risk_level,
                "cycle_seconds":        cycle_sec,
                "stop_loss_pct":        stop_loss_pct,
                "trailing_stop_pct":    trailing_stop_pct,
                "sell_cooldown_cycles": sell_cooldown_cycles,
                **det_params,
            },
            "initial_holdings": initial_holdings,
            "resume":           resume,
            "started":          False,
            "started_at":       datetime.utcnow().isoformat(),
        })
        return jsonify({
            "ok":           True,
            "session_id":   session_id,
            "budget":       budget,
            "risk_level":   risk_level,
            "cycle_seconds": cycle_sec,
            "decider":      decider,
            "resume_failed": resume_failed,
            "serverless":   True,
        })

    _spawn_session(session_id, session_name, budget, run_cfg,
                   resume=resume, initial_holdings=initial_holdings, decider=decider)
    return jsonify({
        "ok":           True,
        "session_id":   session_id,
        "budget":       budget,
        "risk_level":   risk_level,
        "cycle_seconds": cycle_sec,
        "decider":      decider,
        "resume_failed": resume_failed,
    })


def _liquidate_session(session_id: str, params: dict, session_name: str) -> dict:
    """Force-sell every position of `session_id` to USDC at current Binance prices.

    SIMULATION-ONLY: uses sim_engine.run(liquidate_at_end=True), which does
    paper trading — fetches Binance prices for valuation but never sends
    real sell orders. For real-mode "tout vendre" use /api/trade/liquidate.

    Skips if no holdings.
    """
    from db.store import get_state

    from .. import simulation as sim_engine

    last_state = get_state("simulation") or {}
    if last_state.get("session_id") != session_id:
        return {"skipped": "no state for this session"}
    holdings = last_state.get("holdings") or {}
    if not holdings:
        return {"skipped": "no holdings to liquidate"}

    cfg     = load_config()
    budget  = float(params.get("budget") or cfg.get("budget", 100))
    run_cfg = {**cfg, **{k: v for k, v in params.items() if v is not None}}
    cur_cyc = int(last_state.get("cycle", 0))

    local_stop = threading.Event()
    def _short_circuit(_c: int, _s: dict) -> None:
        local_stop.set()

    # max_cycles=cur_cyc + liquidate_at_end=True makes effective_max = cur_cyc+1,
    # so the next loop iteration runs the liquidation block then breaks.
    sim_engine.run(
        budget,
        config=run_cfg,
        on_cycle=_short_circuit,
        stop_event=local_stop,
        resume=True,
        max_cycles=cur_cyc,
        liquidate_at_end=True,
        session_id=session_id,
        session_name=session_name,
    )
    return {"liquidated_symbols": list(holdings.keys()), "from_cycle": cur_cyc}


@bp.post("/api/simulation/stop")
def sim_stop():
    """Stop ONE session (by id) — others keep running, fully independent."""
    body = request.json or {}
    session_id = body.get("session_id") or request.args.get("session_id")
    liquidation_result = None
    cleaned_logs: int | None = None

    if _IS_SERVERLESS:
        active = _read_active_sims()
        # Default to the only active session if id omitted.
        if not session_id and len(active) == 1:
            session_id = next(iter(active))
        entry = active.get(session_id) if session_id else None
        if entry:
            try:
                liquidation_result = _liquidate_session(
                    session_id=session_id,
                    params=entry.get("params") or {},
                    session_name=entry.get("session_name", ""),
                )
            except Exception:
                log.exception("Liquidation finale échouée")
                liquidation_result = {"error": "liquidation failed"}
        _remove_active_sim(session_id) if session_id else None
        cleaned_logs = _cleanup_sim_technical_logs(session_id)
        return jsonify({"ok": True, "liquidation": liquidation_result,
                        "cleaned_technical_logs": cleaned_logs})

    # Local / threaded mode: stop that session's loop, wait for exit, liquidate.
    import time as _time
    entry = _sim_registry.get(session_id) if session_id else None
    if not entry:
        # Back-compat: if a single session is running and no id given, stop it.
        running = _sim_registry.running_ids()
        if not session_id and len(running) == 1:
            session_id = running[0]
            entry = _sim_registry.get(session_id)
    if not entry:
        return jsonify({"error": "Session introuvable"}), 404

    state = entry["state"]
    sname = state.session_name
    cycle_sec = state.cycle_seconds
    entry["stop_event"].set()
    state.stop()

    deadline = _time.time() + 15
    while state.running and _time.time() < deadline:
        _time.sleep(0.2)

    if session_id:
        try:
            liquidation_result = _liquidate_session(
                session_id=session_id,
                params={"cycle_seconds": cycle_sec},
                session_name=sname,
            )
        except Exception:
            log.exception("Liquidation finale échouée")
            liquidation_result = {"error": "liquidation failed"}

    _sim_registry.remove(session_id)
    cleaned_logs = _cleanup_sim_technical_logs(session_id)
    return jsonify({"ok": True, "liquidation": liquidation_result,
                    "cleaned_technical_logs": cleaned_logs})


def _cleanup_sim_technical_logs(session_id: str) -> int | None:
    """Une fois la simulation arrêtée, purger ses logs techniques.

    On garde les catégories `trade` et `market` (utiles pour le post-mortem
    et l'optimisation du modèle) et on supprime la catégorie `technical`
    (bootstrap de cycle, cash/positions, dumps internes).
    """
    if not session_id:
        return None
    try:
        from db.store import clean_logs
        deleted = clean_logs(
            older_than_days=0,
            mode="simulation",
            session_id=session_id,
            category="technical",
        )
        log.info("[SIM] Logs techniques purgés pour session %s: %d", session_id, deleted)
        return deleted
    except Exception:
        log.warning("Impossible de purger les logs techniques", exc_info=True)
        return None


@bp.get("/api/simulation/sessions")
def sim_sessions():
    try:
        from db.store import list_simulation_sessions, list_simulation_sessions_v2
        try:
            return jsonify(list_simulation_sessions_v2())
        except Exception:
            log.warning("list_simulation_sessions_v2 a échoué, repli sur v1", exc_info=True)
            return jsonify(list_simulation_sessions())
    except Exception:
        log.exception("Erreur sim_sessions")
        return jsonify({"error": "Erreur lors du chargement des sessions"}), 500


@bp.get("/api/real/sessions")
def real_sessions():
    """List real-mode sessions (one record per Resume→Stop cycle).

    Also returns the currently active session id (if any) so the frontend
    can put a green pulse on the right card. Sessions table is the
    authoritative source; trades pre-dating session-per-run remain visible
    via the catch-all real history.
    """
    try:
        from db.store import get_state, list_real_sessions
        sessions = []
        try:
            sessions = list_real_sessions()
        except Exception:
            log.warning("list_real_sessions a échoué", exc_info=True)
        active_sid = get_state("active_real_session_id") or None
        return jsonify({"sessions": sessions, "active_session_id": active_sid})
    except Exception:
        log.exception("Erreur real_sessions")
        return jsonify({"error": "Erreur lors du chargement des sessions réelles"}), 500


@bp.patch("/api/simulation/sessions/<session_id>")
def sim_session_rename(session_id: str):
    body = request.json or {}
    name = body.get("name", "").strip()
    if not name:
        return jsonify({"error": "name requis"}), 400
    try:
        from db.store import rename_session
        rename_session(session_id, name)
        return jsonify({"ok": True})
    except Exception:
        log.exception("Erreur sim_session_rename")
        return jsonify({"error": "Erreur lors du renommage de la session"}), 500


@bp.delete("/api/simulation/sessions/<session_id>")
def sim_session_delete(session_id: str):
    # Protect against deleting a session that's currently running — sim or
    # real. For real, the source of truth is ``active_real_session_id`` in
    # the DB; deleting the active record would leave the runner pointing at
    # a phantom session.
    entry = _sim_registry.get(session_id)
    if (entry and entry["state"].running) or session_id in _read_active_sims():
        return jsonify({"error": "Impossible de supprimer la session en cours d'exécution. Arrête-la d'abord."}), 409
    try:
        from db.store import get_state as _get_state
        if (_get_state("active_real_session_id") or None) == session_id:
            return jsonify({"error": "Cette session réelle est encore armée. Clique Arrêter sur Activité réelle d'abord."}), 409
    except Exception:
        pass
    try:
        from db.store import delete_session, set_state
        delete_session(session_id)
        # Invalidate auto-resume so we don't try to restart a deleted session
        try:
            saved = None
            from db.store import get_state
            saved = get_state("simulation_state")
            if isinstance(saved, dict) and saved.get("session_id") == session_id:
                set_state("simulation_state", None)
        except Exception:
            log.warning("Impossible de purger l'état auto-resume", exc_info=True)
        return jsonify({"ok": True})
    except Exception:
        log.exception("Erreur sim_session_delete")
        return jsonify({"error": "Erreur lors de la suppression de la session"}), 500


@bp.get("/api/simulation/sessions/<session_id>/detail")
def sim_session_detail(session_id: str):
    try:
        from db.store import get_session
        d = get_session(session_id)
        if not d:
            return jsonify({"error": "Session non trouvée"}), 404
        if d.get("initial_state"):
            try:
                d["initial_state"] = json.loads(d["initial_state"])
            except Exception:
                log.warning("Impossible de parser initial_state pour session %s", session_id, exc_info=True)
        return jsonify(d)
    except Exception:
        log.exception("Erreur sim_session_detail")
        return jsonify({"error": "Erreur lors du chargement du détail de session"}), 500
