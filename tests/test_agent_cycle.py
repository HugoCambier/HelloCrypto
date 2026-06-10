"""Integration test for the real-mode cycle's stop-loss execution path.

Guards the agent's *consumption* of check_stops: the loop in _execute_cycle
iterates StopSignal dataclasses (attribute access), not tuples. A regression
here only surfaces in prod (run_one_cycle), never in the trading unit tests.
"""
from __future__ import annotations

from hellocrypto import agent
from hellocrypto.api import NotionalTooSmall


def _base_cfg():
    return {
        "watchlist": ["BTCUSDC"],
        "budget": 1000,
        "stop_loss_pct": 10,
        "trailing_stop_pct": 5,
        "cycle_seconds": 60,
        "decider": "deterministic",
    }


def _patch_cycle_io(monkeypatch, positions, *, market_sell):
    monkeypatch.setattr(agent, "get_open_positions", lambda _wl: positions)
    monkeypatch.setattr(agent, "get_balance", lambda _a: 500.0)
    monkeypatch.setattr(agent, "_fetch_market_data", lambda _wl, _cs: {"BTCUSDC": {"price": 80.0}})
    monkeypatch.setattr(agent, "get_fear_and_greed", lambda: None)
    monkeypatch.setattr(agent, "get_btc_dominance", lambda: None)
    monkeypatch.setattr(agent, "_capture_snapshots", lambda *a, **k: None)
    monkeypatch.setattr(agent.strategy, "update_peak_prices", lambda *a, **k: None)
    monkeypatch.setattr(agent, "get_ticker", lambda _s: 80.0)
    monkeypatch.setattr(agent, "save_trade", lambda *a, **k: None)
    monkeypatch.setattr(agent, "market_sell", market_sell)
    monkeypatch.setattr(
        agent, "regime_decision",
        lambda **k: ({"actions": [], "market_sentiment": "", "summary": ""}, {}),
    )


def test_execute_cycle_sells_position_hitting_stop_loss(monkeypatch):
    positions = {"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}}
    sells: list[tuple] = []

    def fake_sell(sym, qty):
        sells.append((sym, qty))
        return {}, 0.0, "USDC"

    _patch_cycle_io(monkeypatch, positions, market_sell=fake_sell)

    new_state = agent._execute_cycle(
        cfg=_base_cfg(), cycle=1, last_llm_call=0.0, llm_call_count=0,
        ref_prices={}, recent_decisions=[], peak_prices={}, cooldown_map={},
    )

    # -20% < -10% stop → the position is liquidated via market_sell.
    assert sells == [("BTCUSDC", 1.0)]
    assert "BTCUSDC" not in positions
    assert new_state["cooldown_map"]["BTCUSDC"] == 1


def test_execute_cycle_skips_dust_position_on_stop_loss(monkeypatch):
    """A position below MIN_NOTIONAL can't be sold; the cycle must not crash."""
    positions = {"BTCUSDC": {"qty": 0.00001, "avg_price": 100.0}}

    def dust_sell(_sym, _qty):
        raise NotionalTooSmall("BTCUSDC")

    _patch_cycle_io(monkeypatch, positions, market_sell=dust_sell)

    new_state = agent._execute_cycle(
        cfg=_base_cfg(), cycle=1, last_llm_call=0.0, llm_call_count=0,
        ref_prices={}, recent_decisions=[], peak_prices={}, cooldown_map={},
    )

    # Dust dropped from tracking, no trade recorded, cycle completes cleanly.
    assert "BTCUSDC" not in positions
    assert "BTCUSDC" not in new_state["cooldown_map"]
