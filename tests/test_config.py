"""load_config source-of-truth: the DB is authoritative, file is fallback.

Regression guard for the bug where a committed config.json (GitHub Actions
cron checkout) shadowed UI changes persisted to the DB — silently reverting
``decider`` (and any other UI-set field) to its checked-in value.
"""
from __future__ import annotations

import json

import db.store as store
import hellocrypto.api as api


def test_load_config_prefers_db_over_file(monkeypatch, tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"decider": "llm", "risk_level": 7}))
    monkeypatch.setattr(api, "CONFIG_FILE", f)
    monkeypatch.setattr(store, "get_state",
                        lambda k: {"decider": "deterministic", "risk_level": 7} if k == "config" else None)
    assert api.load_config()["decider"] == "deterministic"


def test_load_config_falls_back_to_file_when_db_has_no_config(monkeypatch, tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"decider": "deterministic"}))
    monkeypatch.setattr(api, "CONFIG_FILE", f)
    monkeypatch.setattr(store, "get_state", lambda k: None)
    assert api.load_config()["decider"] == "deterministic"


def test_load_config_falls_back_to_file_when_db_errors(monkeypatch, tmp_path):
    f = tmp_path / "config.json"
    f.write_text(json.dumps({"decider": "deterministic"}))
    monkeypatch.setattr(api, "CONFIG_FILE", f)

    def _boom(_k):
        raise RuntimeError("DB down")

    monkeypatch.setattr(store, "get_state", _boom)
    assert api.load_config()["decider"] == "deterministic"


def _fake_state(monkeypatch, initial):
    """Patch db.store state accessors with an in-memory dict; return it."""
    state = dict(initial)
    monkeypatch.setattr(store, "get_state", lambda k: state.get(k))
    monkeypatch.setattr(store, "set_state", lambda k, v: state.__setitem__(k, v))
    monkeypatch.setattr(store, "upsert_session",
                        lambda *a, **k: None)
    return state


def test_starting_sim_does_not_stop_running_real(monkeypatch):
    """Regression: a sim start posts mode='simulation' to the shared config; that
    must NOT clear a live real session (they are independent)."""
    from hellocrypto.routes.config import _maybe_toggle_real_session

    state = _fake_state(monkeypatch, {"active_real_session_id": "abc123"})
    # Real was running (enabled=true, mode=real); a sim start flips mode only.
    _maybe_toggle_real_session({"enabled": True, "mode": "real"},
                               {"enabled": True, "mode": "simulation"})
    assert state["active_real_session_id"] == "abc123"


def test_explicit_disable_stops_real(monkeypatch):
    """The 'Désactiver le runner réel' button posts enabled=false → clear pointer."""
    from hellocrypto.routes.config import _maybe_toggle_real_session

    state = _fake_state(monkeypatch, {"active_real_session_id": "abc123"})
    _maybe_toggle_real_session({"enabled": True, "mode": "real"},
                               {"enabled": False, "mode": "real"})
    assert state["active_real_session_id"] is None


def test_arming_real_opens_session(monkeypatch):
    from hellocrypto.routes.config import _maybe_toggle_real_session

    state = _fake_state(monkeypatch, {"active_real_session_id": None})
    _maybe_toggle_real_session({"enabled": False, "mode": "simulation"},
                               {"enabled": True, "mode": "real"})
    assert state["active_real_session_id"]
