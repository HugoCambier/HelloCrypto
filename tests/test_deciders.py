"""Tests for regime_decision stance system."""

from __future__ import annotations

from hellocrypto.deciders import _derive_stance, regime_decision


def _sym(trend: str = "haussier") -> dict:
    # compute_score_rules: base 5 + trend_1d haussier +2 + trend haussier +1
    # + macd hist>0 +1 + sma7>sma25 +1 = 10  (clamped to 10)
    return {
        "trend_1d": trend,
        "trend": "haussier" if trend == "haussier" else "baissier",
        "price": 100.0,
        "macd": {"histogram": 1.0 if trend == "haussier" else -1.0},
        "sma7": 105.0 if trend == "haussier" else 95.0,
        "sma25": 100.0,
    }


def _market(btc_trend: str = "haussier", n_bull: int = 6, n_bear: int = 4) -> dict:
    m: dict = {"BTCUSDC": _sym(trend=btc_trend)}
    for i in range(n_bull):
        m[f"BULL{i}USDC"] = _sym(trend="haussier")
    for i in range(n_bear):
        m[f"BEAR{i}USDC"] = _sym(trend="baissier")
    return m


def test_stance_deploy_in_bull():
    market = _market(btc_trend="haussier", n_bull=6, n_bear=4)
    assert _derive_stance(market) == "DEPLOY"


def test_stance_preserve_in_bear():
    market = _market(btc_trend="baissier", n_bull=6, n_bear=4)
    assert _derive_stance(market) == "PRESERVE"


def test_stance_selective_default():
    # BTC neutre, breadth mixed
    market = _market(btc_trend="neutre", n_bull=5, n_bear=5)
    assert _derive_stance(market) == "SELECTIVE"


def test_stance_selective_btc_bull_but_bear_majority():
    # BTC haussier but bear > bull → SELECTIVE (not DEPLOY)
    market = _market(btc_trend="haussier", n_bull=3, n_bear=7)
    assert _derive_stance(market) == "SELECTIVE"


def test_user_pin_overrides_stance():
    """User-supplied buy_threshold should beat stance even in DEPLOY."""
    market = _market(btc_trend="haussier", n_bull=6, n_bear=2)
    # Symbols score max 10; threshold pinned to 11 (above max) → no buys
    result, _ = regime_decision(
        market_raw=market,
        holdings={},
        cash=1000.0,
        cycle=0,
        now_ts=1_000_000.0,
        risk_level=5,
        params={"decide_every_cycles": 1, "buy_threshold": 11},
    )
    buys = [a for a in result["actions"] if a["type"] == "buy"]
    assert buys == [], "user-pinned threshold 11 should block all entries"
    assert result["stance"] == "DEPLOY"


def test_disable_regime_stance():
    """enable_regime_stance=False → legacy DEFAULTS params, stance=OFF."""
    market = _market(btc_trend="haussier", n_bull=6, n_bear=2)
    result, _ = regime_decision(
        market_raw=market,
        holdings={},
        cash=1000.0,
        cycle=0,
        now_ts=1_000_000.0,
        risk_level=5,
        params={"decide_every_cycles": 1, "enable_regime_stance": False},
    )
    assert result["stance"] == "OFF"
    # Default buy_threshold=8, all symbols score 9 → should get buys
    buys = [a for a in result["actions"] if a["type"] == "buy"]
    assert len(buys) > 0
