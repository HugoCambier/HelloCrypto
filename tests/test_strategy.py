"""Tests for the shared strategy helpers."""
from __future__ import annotations

from hellocrypto import strategy

# ── helpers ───────────────────────────────────────────────────────────────────

def test_update_peak_prices_lifts_peak_when_higher():
    holdings = {"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}}
    peaks = {"BTCUSDC": 105.0}
    strategy.update_peak_prices(holdings, {"BTCUSDC": 110.0}, peaks)
    assert peaks["BTCUSDC"] == 110.0


def test_update_peak_prices_keeps_existing_when_lower():
    peaks = {"BTCUSDC": 110.0}
    strategy.update_peak_prices(
        {"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}},
        {"BTCUSDC": 105.0}, peaks,
    )
    assert peaks["BTCUSDC"] == 110.0


def test_update_peak_prices_initializes_with_current_when_missing():
    peaks: dict = {}
    strategy.update_peak_prices(
        {"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}},
        {"BTCUSDC": 102.0}, peaks,
    )
    assert peaks["BTCUSDC"] == 102.0


def test_in_cooldown_false_for_never_sold_symbol():
    # Fresh start: BTC was never sold → not blocked, even at cycle 1.
    assert not strategy.in_cooldown("BTCUSDC", cycle=1, cooldown_map={}, max_cycles=3)


def test_in_cooldown_true_within_window():
    assert strategy.in_cooldown(
        "BTCUSDC", cycle=5, cooldown_map={"BTCUSDC": 4}, max_cycles=3
    ) is True


def test_in_cooldown_false_after_window():
    assert strategy.in_cooldown(
        "BTCUSDC", cycle=10, cooldown_map={"BTCUSDC": 4}, max_cycles=3
    ) is False


def test_format_buy_reason_adds_horizon_tag():
    out = strategy.format_buy_reason({"horizon": "short", "reason": "RSI bas"})
    assert out == "[SHORT] RSI bas"


def test_format_buy_reason_passes_through_without_horizon():
    out = strategy.format_buy_reason({"reason": "RSI bas"})
    assert out == "RSI bas"


# ── apply_paper_stops ─────────────────────────────────────────────────────────

def test_apply_paper_stops_triggers_hard_stop():
    holdings = {"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}}
    peaks    = {"BTCUSDC": 100.0}
    cooldown: dict = {}
    cash, fees, trades = strategy.apply_paper_stops(
        holdings, {"BTCUSDC": 85.0}, peaks, cooldown,
        stop_loss=0.10, trail_stop=0.05, cycle=3,
    )
    assert len(trades) == 1
    assert trades[0].action == "SELL (stop-loss)"
    assert "BTCUSDC" not in holdings
    assert cooldown["BTCUSDC"] == 3
    assert cash > 80  # received cash after stop fired
    assert fees > 0


# ── apply_paper_actions ───────────────────────────────────────────────────────

def test_apply_paper_actions_executes_buy():
    holdings: dict = {}
    peaks: dict    = {}
    cooldown: dict = {}
    actions = [{"type": "buy", "symbol": "BTCUSDC", "usdc_amount": 50, "horizon": "short"}]
    new_cash, fees, trades = strategy.apply_paper_actions(
        actions=actions,
        holdings=holdings, cash=100.0,
        prices={"BTCUSDC": 50_000.0},
        peak_prices=peaks, cooldown_map=cooldown,
        market_raw={"BTCUSDC": {"rsi14": 30}},
        cycle=1, risk_level=5, sell_cooldown_cycles=3,
    )
    assert len(trades) == 1
    assert trades[0].action == "BUY"
    assert "BTCUSDC" in holdings
    assert new_cash < 100  # cash spent


def test_apply_paper_actions_skips_buy_under_cooldown():
    holdings: dict = {}
    cooldown = {"BTCUSDC": 1}    # vendu au cycle 1
    actions = [{"type": "buy", "symbol": "BTCUSDC", "usdc_amount": 50}]
    _, _, trades = strategy.apply_paper_actions(
        actions=actions, holdings=holdings, cash=100.0,
        prices={"BTCUSDC": 50_000.0},
        peak_prices={}, cooldown_map=cooldown,
        market_raw={}, cycle=2, risk_level=5, sell_cooldown_cycles=3,
    )
    assert trades == []
    assert "BTCUSDC" not in holdings


def test_apply_paper_actions_gates_low_confidence():
    """Phase E: action portant confidence < min_confidence est ignorée."""
    actions = [{"type": "buy", "symbol": "BTCUSDC",
                "usdc_amount": 50, "confidence": 0.2}]
    _, _, trades = strategy.apply_paper_actions(
        actions=actions, holdings={}, cash=100.0,
        prices={"BTCUSDC": 50_000.0},
        peak_prices={}, cooldown_map={},
        market_raw={}, cycle=1, risk_level=5,
        sell_cooldown_cycles=3, min_confidence=0.5,
    )
    assert trades == []


def test_apply_paper_actions_scales_position_by_confidence():
    """Confidence haute → on autorise jusqu'au plein usdc_amount du modèle."""
    high = {"type": "buy", "symbol": "BTCUSDC", "usdc_amount": 100, "confidence": 1.0}
    low  = {"type": "buy", "symbol": "BTCUSDC", "usdc_amount": 100, "confidence": 0.5}
    holdings_h, holdings_l = {}, {}
    new_cash_h, _, _ = strategy.apply_paper_actions(
        actions=[high], holdings=holdings_h, cash=10_000,
        prices={"BTCUSDC": 50_000}, peak_prices={}, cooldown_map={},
        market_raw={"BTCUSDC": {"rsi14": 50}}, cycle=1,
        risk_level=10, sell_cooldown_cycles=3,
    )
    new_cash_l, _, _ = strategy.apply_paper_actions(
        actions=[low], holdings=holdings_l, cash=10_000,
        prices={"BTCUSDC": 50_000}, peak_prices={}, cooldown_map={},
        market_raw={"BTCUSDC": {"rsi14": 50}}, cycle=1,
        risk_level=10, sell_cooldown_cycles=3,
    )
    # High confidence achète plus → cash restant plus bas
    assert new_cash_h < new_cash_l
