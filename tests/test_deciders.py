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


# ── CASH stance (leading-signal bear protection) ─────────────────────────────


def test_stance_cash_on_btc_drawdown():
    """BTC drawdown ≥ 7% from 7d high is a leading signal → CASH."""
    market = _market(btc_trend="haussier", n_bull=6, n_bear=2)
    market["BTCUSDC"]["drawdown_pct_7d"] = 8.5
    assert _derive_stance(market) == "CASH"


def test_stance_cash_on_intraday_breadth_collapse():
    """Intraday `trend` baissier on ≥70% of watchlist → CASH."""
    # 1 BTC haussier + 2 bull haussier + 8 bear baissier = 8/11 ≈ 73% intraday bear
    market = _market(btc_trend="haussier", n_bull=2, n_bear=8)
    assert _derive_stance(market) == "CASH"


def test_cash_blocks_all_buys():
    """CASH stance has top_n=0; no buys regardless of scores."""
    market = _market(btc_trend="haussier", n_bull=2, n_bear=8)
    result, _ = regime_decision(
        market_raw=market, holdings={}, cash=1000.0, cycle=0,
        now_ts=1_000_000.0, risk_level=5,
        params={"decide_every_cycles": 1},
    )
    assert result["stance"] == "CASH"
    buys = [a for a in result["actions"] if a["type"] == "buy"]
    assert buys == []


# ── Per-stance exit signal (1d vs 1h) ────────────────────────────────────────


def test_bear_since_tracked_per_signal():
    """Both bear_since_1d and bear_since_1h are tracked simultaneously."""
    market = {
        "BTCUSDC": _sym(trend="haussier"),
        "FOOUSDC": {"trend_1d": "haussier", "trend": "baissier",
                    "price": 100.0, "macd": {"histogram": 0},
                    "sma7": 100.0, "sma25": 100.0},
        "BARUSDC": {"trend_1d": "baissier", "trend": "haussier",
                    "price": 100.0, "macd": {"histogram": 0},
                    "sma7": 100.0, "sma25": 100.0},
    }
    now = 1_000_000.0
    _, st = regime_decision(
        market_raw=market,
        holdings={"FOOUSDC": {"qty": 1.0}, "BARUSDC": {"qty": 1.0}},
        cash=0, cycle=0, now_ts=now,
        params={"decide_every_cycles": 1},
    )
    # FOO: intraday bear only → in _1h tracker only
    assert "FOOUSDC" in st["bear_since_1h"]
    assert "FOOUSDC" not in st["bear_since_1d"]
    # BAR: daily bear only → in _1d tracker only
    assert "BARUSDC" not in st["bear_since_1h"]
    assert "BARUSDC" in st["bear_since_1d"]


def test_preserve_exits_on_intraday_signal():
    """PRESERVE stance triggers exits on intraday `trend`, not the daily SMA."""
    market = {
        # BTC trend_1d baissier → PRESERVE. BTC trend haussier and no drawdown
        # so CASH is NOT triggered.
        "BTCUSDC": {"trend_1d": "baissier", "trend": "haussier",
                    "price": 100.0, "drawdown_pct_7d": 0.0,
                    "macd": {"histogram": 0}, "sma7": 100.0, "sma25": 100.0},
        # FOO: daily still bullish, but intraday flipped bear.
        "FOOUSDC": {"trend_1d": "haussier", "trend": "baissier",
                    "price": 100.0, "macd": {"histogram": 0},
                    "sma7": 100.0, "sma25": 100.0},
    }
    now = 1_000_000.0
    # FOO has been intraday-bear for 25h, entered 30h ago.
    state = {
        "bear_since_1h": {"FOOUSDC": now - 25 * 3600},
        "bear_since_1d": {},
        "entry_ts":      {"FOOUSDC": now - 30 * 3600},
    }
    result, _ = regime_decision(
        market_raw=market, holdings={"FOOUSDC": {"qty": 1.0}}, cash=0,
        cycle=0, now_ts=now,
        params={"decide_every_cycles": 1,
                "trend_confirm_hours": 24.0, "min_hold_hours": 12.0},
        strat_state=state,
    )
    assert result["stance"] == "PRESERVE"
    sells = [a for a in result["actions"] if a["type"] == "sell"]
    assert [s["symbol"] for s in sells] == ["FOOUSDC"]
    assert "trend baissier" in sells[0]["reason"]


def test_deploy_does_not_exit_on_intraday_alone():
    """DEPLOY ignores intraday-only bear: needs daily SMA flip for exit."""
    market = {
        "BTCUSDC": _sym(trend="haussier"),
        # FOO: intraday bear, but daily still bullish.
        "FOOUSDC": {"trend_1d": "haussier", "trend": "baissier",
                    "price": 100.0, "macd": {"histogram": 0},
                    "sma7": 100.0, "sma25": 100.0},
        "BARUSDC": _sym(trend="haussier"),
    }
    now = 1_000_000.0
    state = {
        "bear_since_1h": {"FOOUSDC": now - 100 * 3600},  # long intraday bear
        "bear_since_1d": {},
        "entry_ts":      {"FOOUSDC": now - 100 * 3600},
    }
    result, _ = regime_decision(
        market_raw=market, holdings={"FOOUSDC": {"qty": 1.0}}, cash=0,
        cycle=0, now_ts=now,
        params={"decide_every_cycles": 1,
                "trend_confirm_hours": 24.0, "min_hold_hours": 12.0},
        strat_state=state,
    )
    assert result["stance"] == "DEPLOY"
    assert [a for a in result["actions"] if a["type"] == "sell"] == []


def test_legacy_bear_since_migrated():
    """Old strat_state with `bear_since` key is read as bear_since_1d once."""
    market = {
        "BTCUSDC": _sym(trend="haussier"),
        "FOOUSDC": {"trend_1d": "baissier", "trend": "haussier",
                    "price": 100.0, "macd": {"histogram": 0},
                    "sma7": 100.0, "sma25": 100.0},
    }
    now = 1_000_000.0
    legacy_state = {"bear_since": {"FOOUSDC": now - 100 * 3600},
                    "entry_ts":   {"FOOUSDC": now - 100 * 3600}}
    _, st = regime_decision(
        market_raw=market, holdings={"FOOUSDC": {"qty": 1.0}}, cash=0,
        cycle=0, now_ts=now,
        params={"decide_every_cycles": 1},
        strat_state=legacy_state,
    )
    # Legacy key gone, value migrated into the daily tracker.
    assert "bear_since" not in st
    assert "FOOUSDC" in st["bear_since_1d"]
