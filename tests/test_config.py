"""load_config source-of-truth: the DB is authoritative, file is fallback.

Regression guard for the bug where a committed config.json (GitHub Actions
cron checkout) shadowed UI changes persisted to the DB — silently reverting
``decider`` (and any other UI-set field) to its checked-in value.
"""
from __future__ import annotations

import json

import hellocrypto.api as api
import db.store as store


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
