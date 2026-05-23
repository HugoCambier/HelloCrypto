"""Tests for metric helpers in hellocrypto/eval/metrics.py."""
from __future__ import annotations

import pytest

from hellocrypto.eval.metrics import (
    alpha_vs_btc,
    btc_buy_and_hold_pct,
    max_drawdown_pct,
    sharpe,
    summarize,
    total_return_pct,
    win_rate_pct,
)


def test_total_return_pct():
    assert total_return_pct(100, 110) == pytest.approx(10.0)
    assert total_return_pct(100, 90)  == pytest.approx(-10.0)
    assert total_return_pct(0, 100)   == 0.0  # guard against div/0


def test_btc_buy_and_hold_pct():
    assert btc_buy_and_hold_pct(50_000, 60_000) == pytest.approx(20.0)


def test_alpha_vs_btc():
    assert alpha_vs_btc(15.0, 10.0) == pytest.approx(5.0)
    assert alpha_vs_btc(-5.0, 10.0) == pytest.approx(-15.0)


def test_max_drawdown_simple():
    # 100 → 110 → 90 → 120 : worst drop is from 110 to 90 ≈ -18.18 %
    assert max_drawdown_pct([100, 110, 90, 120]) == pytest.approx(-18.1818, rel=1e-3)


def test_max_drawdown_monotonic_up():
    assert max_drawdown_pct([100, 105, 110, 120]) == 0.0


def test_sharpe_constant_series_is_none():
    # No volatility → Sharpe undefined
    assert sharpe([100, 100, 100, 100]) is None


def test_sharpe_positive_for_upward_drift():
    series = [100 * (1.001 ** i) for i in range(50)]
    s = sharpe(series, cycles_per_year=8760)
    assert s is not None and s > 0


def test_win_rate_pct():
    sells = [
        {"pnl": 10}, {"pnl": -5}, {"pnl": 3}, {"pnl": 0}, {"pnl": None},
    ]
    # 2 winners (10, 3) over 4 closed trades (the None one is ignored)
    assert win_rate_pct(sells) == pytest.approx(50.0)


def test_summarize_keys_present():
    r = summarize(
        initial_value=1000, final_value=1100,
        btc_initial=50_000, btc_final=52_000,
        value_series=[1000, 1050, 1100],
        sells=[{"pnl": 50}, {"pnl": 50}],
        total_fees=2.5, num_trades=4,
        tokens_in=1000, tokens_out=500, cycle_seconds=3600,
    )
    for k in ("return_pct", "btc_return_pct", "alpha_vs_btc_pct",
              "max_drawdown_pct", "sharpe", "win_rate_pct",
              "num_trades", "total_fees", "tokens_total"):
        assert k in r
    assert r["return_pct"] == pytest.approx(10.0)
    assert r["btc_return_pct"] == pytest.approx(4.0)
    assert r["alpha_vs_btc_pct"] == pytest.approx(6.0)
    assert r["tokens_total"] == 1500
