"""Simulation routes + SimState thread-safe container."""
from __future__ import annotations

import copy
import json
import logging
import os
import threading
import uuid
from datetime import datetime
from pathlib import Path

from flask import Blueprint, jsonify, request

from ..api import load_config
from .. import simulation as sim_engine
from .shared import _ROOT

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
            return {
                "running":          self.running,
                "session_id":       self.session_id,
                "session_name":     self.session_name,
                "cycle_seconds":    self.cycle_seconds,
                "cycle_started_at": self.cycle_started_at,
                "snapshot":         copy.deepcopy(self.snapshot),
                "error":            self.error,
            }


_sim_state      = SimState()
_sim_stop_event = threading.Event()

# Vercel can't run long-lived threads (function dies after each HTTP response).
# When detected, the dashboard persists a flag in DB; the GitHub Actions runner
# picks it up and executes cycles. See [[vercel-serverless-sim]].
_IS_SERVERLESS  = bool(os.getenv("VERCEL"))


def _read_active_sim() -> dict | None:
    try:
        from db.store import get_state
        v = get_state("active_sim")
        return v if isinstance(v, dict) else None
    except Exception:
        return None


def _write_active_sim(state: dict | None) -> None:
    try:
        from db.store import set_state
        set_state("active_sim", state)
    except Exception:
        log.warning("Impossible d'écrire active_sim", exc_info=True)


def _serverless_status_dict() -> dict:
    active = _read_active_sim()
    try:
        from db.store import get_state
        snap_data = get_state("simulation") or {}
    except Exception:
        snap_data = {}

    if active:
        params = active.get("params", {})
        return {
            "running":          True,
            "session_id":       active.get("session_id", ""),
            "session_name":     active.get("session_name", ""),
            "cycle_seconds":    int(params.get("cycle_seconds") or 60),
            "cycle_started_at": snap_data.get("saved_at"),
            "snapshot":         snap_data,
            "error":            None,
        }
    return {
        "running":          False,
        "session_id":       snap_data.get("session_id", ""),
        "session_name":     snap_data.get("session_name", ""),
        "cycle_seconds":    int((snap_data.get("params") or {}).get("cycle_seconds") or 60),
        "cycle_started_at": snap_data.get("saved_at"),
        "snapshot":         snap_data,
        "error":            None,
    }

# ── Cloud Scheduler keep-alive (keeps container alive during simulation) ──────

_GCP_PROJECT      = os.getenv("GOOGLE_CLOUD_PROJECT")
_SCHEDULER_REGION = os.getenv("SCHEDULER_REGION", "europe-west1")
_KEEPALIVE_JOB    = os.getenv("KEEPALIVE_JOB", "hellocrypto-keepalive")


def _set_keepalive(enabled: bool) -> None:
    """Enable or disable the Cloud Scheduler keepalive job."""
    if not _GCP_PROJECT:
        return
    try:
        import google.auth
        import google.auth.transport.requests as google_req
        import requests as _req
        creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
        creds.refresh(google_req.Request())
        action = "resume" if enabled else "pause"
        url = (f"https://cloudscheduler.googleapis.com/v1/projects/{_GCP_PROJECT}"
               f"/locations/{_SCHEDULER_REGION}/jobs/{_KEEPALIVE_JOB}:{action}")
        _req.post(url, headers={"Authorization": f"Bearer {creds.token}"}, timeout=10)
        log.info("[SIM] Keep-alive scheduler → %s", action)
    except Exception as exc:
        log.warning("[SIM] Keep-alive scheduler %s échoué: %s",
                    "resume" if enabled else "pause", exc)


# ── Auto-resume on cold start ─────────────────────────────────────────────────

def _try_auto_resume() -> None:
    """If a simulation was running when the container was killed, restart it."""
    if _sim_state.running:
        return
    saved = sim_engine._load_state()
    if not saved or not saved.get("session_id") or not saved.get("running"):
        return

    cfg       = load_config()
    cycle_sec = int(cfg.get("cycle_seconds", 60))
    sid       = saved["session_id"]
    sname     = saved.get("session_name", "auto-resume")
    budget    = saved.get("budget", float(cfg.get("budget", 100)))

    log.info("[SIM] Auto-resume session %s depuis cycle %d", sid, saved.get("cycle", 0))

    global _sim_stop_event
    _sim_stop_event = threading.Event()
    _sim_state.start(sid, sname, cycle_sec)

    def _run():
        try:
            result = sim_engine.run(
                budget,
                config=cfg,
                on_cycle=lambda _c, snap: _sim_state.update_cycle(snap),
                stop_event=_sim_stop_event,
                resume=True,
                session_id=sid,
                session_name=sname,
            )
            _sim_state.finish(result)
        except Exception as exc:
            log.exception("[SIM] Crash auto-resume")
            _sim_state.fail(exc)
        finally:
            _set_keepalive(False)

    _set_keepalive(True)
    threading.Thread(target=_run, daemon=True).start()


_auto_resumed = False


# ── Routes ────────────────────────────────────────────────────────────────────

@bp.get("/api/simulation/keepalive")
def sim_keepalive():
    """Called by Cloud Scheduler to keep the container alive during a run."""
    global _auto_resumed
    if not _auto_resumed:
        _auto_resumed = True
        _try_auto_resume()
    return jsonify({"running": _sim_state.running})


@bp.get("/api/simulation/status")
def sim_status():
    if _IS_SERVERLESS:
        return jsonify(_serverless_status_dict())
    global _auto_resumed
    if not _auto_resumed:
        _auto_resumed = True
        _try_auto_resume()
    return jsonify(_sim_state.to_dict())


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
    global _sim_stop_event
    # On serverless, "running" is tracked via DB flag instead of in-memory state.
    if _IS_SERVERLESS and _read_active_sim():
        return jsonify({"error": "Simulation déjà en cours"}), 409
    if not _IS_SERVERLESS and _sim_state.running:
        return jsonify({"error": "Simulation déjà en cours"}), 409

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
    resume               = bool(body.get("resume", False))
    from_binance         = bool(body.get("from_binance", False))
    max_cycles_raw       = body.get("max_cycles")
    max_cycles           = int(max_cycles_raw) if max_cycles_raw and int(max_cycles_raw) > 0 else None
    liquidate_at_end     = bool(body.get("liquidate_at_end", False))
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
            from hellocrypto.api import get_open_positions as _get_pos, get_balance as _get_bal
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

    # If resume requested but no saved state → downgrade silently + flag it
    resume_failed = False
    if resume:
        from hellocrypto.simulation import _load_state as _sim_load
        if not _sim_load():
            resume = False
            resume_failed = True

    run_cfg      = {**cfg, "risk_level": risk_level, "cycle_seconds": cycle_sec,
                    "stop_loss_pct": stop_loss_pct, "trailing_stop_pct": trailing_stop_pct,
                    "sell_cooldown_cycles": sell_cooldown_cycles}
    session_id   = uuid.uuid4().hex[:8]
    session_name = body.get("session_name") or datetime.utcnow().strftime("%Y-%m-%d %H:%M")
    _sim_stop_event = threading.Event()
    _sim_state.start(session_id, session_name, cycle_sec)

    try:
        from db.store import upsert_session
        upsert_session(session_id, session_name, mode="simulation",
                       initial_state={
                           "budget":               budget,
                           "initial_holdings":     initial_holdings,
                           "risk_level":           risk_level,
                           "cycle_seconds":        cycle_sec,
                           "stop_loss_pct":        stop_loss_pct,
                           "trailing_stop_pct":    trailing_stop_pct,
                           "sell_cooldown_cycles": sell_cooldown_cycles,
                       })
    except Exception:
        log.warning("Impossible de sauvegarder la session dans la base", exc_info=True)

    if _IS_SERVERLESS:
        # Persist the request — the GitHub Actions cron runner will pick it up
        # and execute one cycle per fire, gated by cycle_seconds.
        _write_active_sim({
            "session_id":       session_id,
            "session_name":     session_name,
            "params": {
                "budget":               budget,
                "risk_level":           risk_level,
                "cycle_seconds":        cycle_sec,
                "stop_loss_pct":        stop_loss_pct,
                "trailing_stop_pct":    trailing_stop_pct,
                "sell_cooldown_cycles": sell_cooldown_cycles,
            },
            "initial_holdings": initial_holdings,
            "max_cycles":       max_cycles,
            "liquidate_at_end": liquidate_at_end,
            "resume":           resume,
            "started":          False,
            "started_at":       datetime.utcnow().isoformat(),
        })
        return jsonify({
            "ok":           True,
            "budget":       budget,
            "risk_level":   risk_level,
            "cycle_seconds": cycle_sec,
            "resume_failed": resume_failed,
            "serverless":   True,
        })

    def _run():
        try:
            result = sim_engine.run(
                budget,
                config=run_cfg,
                on_cycle=lambda _cycle, snap: _sim_state.update_cycle(snap),
                stop_event=_sim_stop_event,
                resume=resume,
                max_cycles=max_cycles,
                initial_holdings=initial_holdings if not resume else None,
                session_id=session_id,
                session_name=session_name,
                liquidate_at_end=liquidate_at_end,
            )
            _sim_state.finish(result)
        except Exception as exc:
            log.exception("[SIM] Crash du thread de simulation")
            _sim_state.fail(exc)
        finally:
            _set_keepalive(False)

    _set_keepalive(True)
    threading.Thread(target=_run, daemon=True).start()
    return jsonify({
        "ok":           True,
        "budget":       budget,
        "risk_level":   risk_level,
        "cycle_seconds": cycle_sec,
        "resume_failed": resume_failed,
    })


@bp.post("/api/simulation/stop")
def sim_stop():
    global _sim_stop_event
    if _IS_SERVERLESS:
        _write_active_sim(None)
        return jsonify({"ok": True})
    _sim_stop_event.set()
    _sim_state.stop()
    _set_keepalive(False)
    return jsonify({"ok": True})


@bp.get("/api/simulation/sessions")
def sim_sessions():
    try:
        from db.store import list_simulation_sessions_v2, list_simulation_sessions
        try:
            return jsonify(list_simulation_sessions_v2())
        except Exception:
            log.warning("list_simulation_sessions_v2 a échoué, repli sur v1", exc_info=True)
            return jsonify(list_simulation_sessions())
    except Exception as exc:
        log.exception("Erreur sim_sessions")
        return jsonify({"error": "Erreur lors du chargement des sessions"}), 500


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
    except Exception as exc:
        log.exception("Erreur sim_session_rename")
        return jsonify({"error": "Erreur lors du renommage de la session"}), 500


@bp.delete("/api/simulation/sessions/<session_id>")
def sim_session_delete(session_id: str):
    # Protect against deleting the currently running session
    if _sim_state.running and _sim_state.session_id == session_id:
        return jsonify({"error": "Impossible de supprimer la session en cours d'exécution. Arrête-la d'abord."}), 409
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
    except Exception as exc:
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
