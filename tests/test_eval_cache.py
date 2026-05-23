"""Tests for the LLM decision cache."""
from __future__ import annotations

import pytest

from hellocrypto.eval import llm_cache


@pytest.fixture(autouse=True)
def _clean_cache():
    llm_cache.clear()
    yield
    llm_cache.clear()


def test_get_returns_none_on_miss():
    assert llm_cache.get("gemini", "flash", "sys", "prompt", 0.0) is None


def test_put_then_get_roundtrip():
    decision = {"actions": [{"type": "hold"}], "market_sentiment": "neutral"}
    usage    = {"in": 100, "out": 50, "total": 150}
    llm_cache.put("gemini", "flash", "sys", "prompt", 0.0, decision, usage)
    got = llm_cache.get("gemini", "flash", "sys", "prompt", 0.0)
    assert got["decision"] == decision
    assert got["usage"] == usage


def test_cache_key_distinguishes_inputs():
    llm_cache.put("gemini", "flash", "sys", "prompt-A", 0.0, {"a": 1})
    llm_cache.put("gemini", "flash", "sys", "prompt-B", 0.0, {"b": 2})
    assert llm_cache.get("gemini", "flash", "sys", "prompt-A", 0.0)["decision"] == {"a": 1}
    assert llm_cache.get("gemini", "flash", "sys", "prompt-B", 0.0)["decision"] == {"b": 2}


def test_temperature_is_part_of_key():
    llm_cache.put("gemini", "flash", "sys", "p", 0.0, {"v": "cold"})
    llm_cache.put("gemini", "flash", "sys", "p", 1.0, {"v": "warm"})
    assert llm_cache.get("gemini", "flash", "sys", "p", 0.0)["decision"]["v"] == "cold"
    assert llm_cache.get("gemini", "flash", "sys", "p", 1.0)["decision"]["v"] == "warm"
