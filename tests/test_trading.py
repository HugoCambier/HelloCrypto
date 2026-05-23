"""Unit tests for trading primitives (pure functions, no I/O)."""
from __future__ import annotations

import pytest

from hellocrypto.trading import (
    FEE_RATE,
    check_position_timeouts,
    check_stops,
    check_take_profits,
    compute_position_size,
    paper_buy,
    paper_sell,
)

# ── paper_buy / paper_sell ────────────────────────────────────────────────────

def test_paper_buy_creates_position_with_fee_applied():
    holdings: dict = {}
    res = paper_buy("BTCUSDC", 100.0, 50_000.0, holdings)
    assert res.fee == pytest.approx(100.0 * FEE_RATE)
    expected_qty = (100.0 - res.fee) / 50_000.0
    assert holdings["BTCUSDC"]["qty"] == pytest.approx(expected_qty)
    assert holdings["BTCUSDC"]["avg_price"] == pytest.approx(50_000.0)


def test_paper_buy_averages_existing_position():
    holdings = {"ETHUSDC": {"qty": 1.0, "avg_price": 1000.0}}
    paper_buy("ETHUSDC", 1000.0, 2000.0, holdings)
    # 1.0 @1000 + ~0.4995 @2000 → weighted avg ~ 1332
    assert 1300 < holdings["ETHUSDC"]["avg_price"] < 1400


def test_paper_sell_returns_net_usdc_and_removes_dust_position():
    holdings = {"BTCUSDC": {"qty": 0.001, "avg_price": 50_000.0}}
    res = paper_sell("BTCUSDC", 0.001, 60_000.0, holdings)
    gross = 0.001 * 60_000.0
    assert res.received == pytest.approx(gross * (1 - FEE_RATE))
    assert "BTCUSDC" not in holdings  # qty dust → deleted


def test_paper_sell_partial_keeps_position():
    holdings = {"BTCUSDC": {"qty": 0.01, "avg_price": 50_000.0}}
    paper_sell("BTCUSDC", 0.005, 60_000.0, holdings)
    assert holdings["BTCUSDC"]["qty"] == pytest.approx(0.005)


def test_paper_sell_with_no_position_is_noop():
    holdings: dict = {}
    res = paper_sell("BTCUSDC", 1.0, 50_000.0, holdings)
    assert res.qty == 0
    assert res.received == 0


# ── check_stops ───────────────────────────────────────────────────────────────

def test_hard_stop_loss_triggers_below_threshold():
    holdings = {"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}}
    sigs = check_stops(holdings, {"BTCUSDC": 85.0}, {}, stop_loss=0.10, trail_stop=0.05)
    assert len(sigs) == 1
    assert sigs[0].kind == "stop-loss"


def test_no_stop_above_threshold():
    holdings = {"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}}
    sigs = check_stops(holdings, {"BTCUSDC": 95.0}, {}, stop_loss=0.10, trail_stop=0.05)
    assert sigs == []


def test_trailing_stop_triggers_after_peak():
    holdings = {"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}}
    # peak 120, current 113 → -5.8% from peak, still above entry
    sigs = check_stops(holdings, {"BTCUSDC": 113.0}, {"BTCUSDC": 120.0},
                       stop_loss=0.20, trail_stop=0.05)
    assert len(sigs) == 1
    assert sigs[0].kind == "trailing-stop"


def test_trailing_stop_does_not_trigger_below_entry():
    """Trailing stop is only active once we're profitable AND peak > entry."""
    holdings = {"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}}
    sigs = check_stops(holdings, {"BTCUSDC": 95.0}, {"BTCUSDC": 102.0},
                       stop_loss=0.20, trail_stop=0.05)
    assert sigs == []


# ── compute_position_size ─────────────────────────────────────────────────────

def test_position_size_clamps_to_cash_max_pct():
    # risk_level 5 → 25% cap. RSI neutral (~50) → factor ~1.0
    size = compute_position_size(usdc_requested=10_000, cash=100, risk_level=5, rsi=50)
    assert 20 <= size <= 30


def test_position_size_low_rsi_increases_allocation():
    high_rsi = compute_position_size(1000, 100, risk_level=5, rsi=80)
    low_rsi  = compute_position_size(1000, 100, risk_level=5, rsi=20)
    assert low_rsi > high_rsi


def test_position_size_uses_llm_amount_when_smaller_than_cap():
    # risk 10 → cap = 45% of 100 = 45. LLM asks 10 → returns 10.
    assert compute_position_size(10, 100, risk_level=10, rsi=50) == pytest.approx(10)


# ── check_take_profits ────────────────────────────────────────────────────────

def test_take_profit_triggers_first_level():
    holdings = {"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}}
    state: dict = {}
    sigs = check_take_profits(
        holdings, {"BTCUSDC": 112.0},
        tp_levels=[{"pct": 0.10, "sell_frac": 0.5}, {"pct": 0.20, "sell_frac": 0.25}],
        tp_state=state,
    )
    assert len(sigs) == 1
    assert sigs[0].level == 1
    assert sigs[0].qty_to_sell == pytest.approx(0.5)


def test_take_profit_does_not_retrigger_same_level():
    holdings = {"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}}
    state = {"BTCUSDC": {1: True}}
    sigs = check_take_profits(
        holdings, {"BTCUSDC": 115.0},
        tp_levels=[{"pct": 0.10, "sell_frac": 0.5}],
        tp_state=state,
    )
    assert sigs == []


# ── check_position_timeouts ───────────────────────────────────────────────────

def test_timeout_closes_stagnant_position():
    holdings = {"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}}
    sigs = check_position_timeouts(
        holdings, {"BTCUSDC": 100.5},  # +0.5% < min_gain_pct=1%
        entry_cycles={"BTCUSDC": 0},
        current_cycle=60, max_hold_cycles=50, min_gain_pct=0.01,
    )
    assert len(sigs) == 1


def test_timeout_skips_profitable_position():
    holdings = {"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}}
    sigs = check_position_timeouts(
        holdings, {"BTCUSDC": 105.0},
        entry_cycles={"BTCUSDC": 0},
        current_cycle=60, max_hold_cycles=50, min_gain_pct=0.01,
    )
    assert sigs == []
