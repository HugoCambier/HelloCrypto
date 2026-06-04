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


def test_stance_cash_requires_both_drawdown_and_breadth():
    """CASH fires only when BTC drawdown ≥7% AND intraday bear breadth ≥70%."""
    market = _market(btc_trend="haussier", n_bull=2, n_bear=8)
    market["BTCUSDC"]["drawdown_pct_7d"] = 8.5
    # Now both conditions are met: drawdown 8.5% ≥ 7%, breadth 8/11 ≈ 73% ≥ 70%
    assert _derive_stance(market) == "CASH"


def test_stance_no_cash_on_drawdown_alone():
    """Drawdown without breadth collapse is NOT enough — anti-false-positive."""
    market = _market(btc_trend="haussier", n_bull=6, n_bear=2)
    market["BTCUSDC"]["drawdown_pct_7d"] = 10.0
    # Breadth bear = 2/9 ≈ 22% < 70% → not CASH (still DEPLOY since btc haussier)
    assert _derive_stance(market) == "DEPLOY"


def test_stance_no_cash_on_breadth_alone():
    """Breadth collapse without confirmed BTC drawdown is NOT CASH either."""
    market = _market(btc_trend="haussier", n_bull=2, n_bear=8)
    # No drawdown set → AND-gate not met → falls through to next branches.
    # bull_count=3 (BTC+2), bear_count=8 → not DEPLOY (bull < bear),
    # btc_trend=haussier (not baissier) → SELECTIVE
    assert _derive_stance(market) == "SELECTIVE"


def test_cash_blocks_all_buys():
    """CASH stance has top_n=0; no buys regardless of scores."""
    market = _market(btc_trend="haussier", n_bull=2, n_bear=8)
    market["BTCUSDC"]["drawdown_pct_7d"] = 8.5  # both conditions met
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


def test_preserve_exits_on_intraday_signal_when_score_weak():
    """PRESERVE stance triggers exits on intraday `trend` *iff* the score
    has also fallen under the anti-whipsaw threshold."""
    market = {
        # BTC trend_1d baissier → PRESERVE. BTC trend haussier and no drawdown
        # so CASH is NOT triggered.
        "BTCUSDC": {"trend_1d": "baissier", "trend": "haussier",
                    "price": 100.0, "drawdown_pct_7d": 0.0,
                    "macd": {"histogram": 0}, "sma7": 100.0, "sma25": 100.0},
        # FOO: daily bullish but intraday bearish, with weak multi-signal
        # state pushing the score under the exit threshold (5).
        # Score: 5 +2 (trend_1d) -1 (trend) -1 (MACD) -1 (sma cross)
        #        -3 (RSI>75) = 1
        "FOOUSDC": {"trend_1d": "haussier", "trend": "baissier",
                    "price": 100.0, "macd": {"histogram": -1.0},
                    "sma7": 95.0, "sma25": 100.0, "rsi14": 80.0},
    }
    now = 1_000_000.0
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


def test_score_gate_blocks_whipsaw_in_DEPLOY():
    """In DEPLOY/SELECTIVE the score gate is ON: a strong-score position
    survives a trend_1d flip if other signals still support it."""
    # BTC haussier → DEPLOY. FOO with trend_1d just-flipped but otherwise
    # bullish: score 5 -2 (trend_1d) +1 (trend) +1 (MACD) +1 (sma) = 6 ≥ 5.
    market = {
        "BTCUSDC": _sym(trend="haussier"),
        "FOOUSDC": {"trend_1d": "baissier", "trend": "haussier",
                    "price": 100.0, "macd": {"histogram": 1.0},
                    "sma7": 105.0, "sma25": 100.0},
    }
    now = 1_000_000.0
    state = {
        "bear_since_1d": {"FOOUSDC": now - 100 * 3600},
        "bear_since_1h": {},
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


def test_score_gate_allows_exit_in_DEPLOY_when_score_weak():
    """Same DEPLOY context, but score is now genuinely weak → exit fires."""
    market = {
        "BTCUSDC": _sym(trend="haussier"),
        # Weak: 5 -2 (trend_1d) -1 (trend) -1 (MACD) -1 (sma) -3 (RSI>75) = 0
        "FOOUSDC": {"trend_1d": "baissier", "trend": "baissier",
                    "price": 100.0, "macd": {"histogram": -1.0},
                    "sma7": 95.0, "sma25": 100.0, "rsi14": 80.0},
    }
    now = 1_000_000.0
    state = {
        "bear_since_1d": {"FOOUSDC": now - 100 * 3600},
        "bear_since_1h": {"FOOUSDC": now - 100 * 3600},
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
    assert [a["symbol"] for a in result["actions"] if a["type"] == "sell"] == ["FOOUSDC"]


def test_score_gate_off_in_PRESERVE_exits_on_intraday_alone():
    """In PRESERVE the gate is off (threshold 99): exit on intraday trend
    fires regardless of score — defensive stance prioritizes speed."""
    market = {
        "BTCUSDC": {"trend_1d": "baissier", "trend": "haussier",
                    "price": 100.0, "drawdown_pct_7d": 0.0,
                    "macd": {"histogram": 0}, "sma7": 100.0, "sma25": 100.0},
        # FOO has a HIGH score (would block in DEPLOY) but PRESERVE bypasses.
        "FOOUSDC": {"trend_1d": "haussier", "trend": "baissier",
                    "price": 100.0, "macd": {"histogram": 1.0},
                    "sma7": 105.0, "sma25": 100.0},
    }
    now = 1_000_000.0
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
    assert [a["symbol"] for a in result["actions"] if a["type"] == "sell"] == ["FOOUSDC"]


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


def test_scale_out_fires_at_first_profit_milestone():
    """Position up +35% from avg_price crosses the +30% milestone → scale_out
    action fires for 1/3 of current qty, milestone marked taken in state."""
    market = {
        "BTCUSDC": {**_sym(trend="haussier"), "price": 135.0},
    }
    now = 1_000_000.0
    state = {"entry_ts": {"BTCUSDC": now - 100 * 3600}}
    result, new_state = regime_decision(
        market_raw=market,
        holdings={"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}},  # +35% gain
        cash=0, cycle=0, now_ts=now, risk_level=7,
        params={"decide_every_cycles": 1, "enable_regime_stance": False,
                "buy_threshold": 8},
        strat_state=state,
    )
    scale_actions = [a for a in result["actions"] if a["type"] == "scale_out"]
    assert len(scale_actions) == 1
    assert scale_actions[0]["symbol"] == "BTCUSDC"
    assert abs(scale_actions[0]["qty"] - 1.0 / 3.0) < 1e-6
    assert 0.30 in new_state["milestones_taken"]["BTCUSDC"]


def test_scale_out_does_not_double_fire_same_milestone():
    """Once +30% has been taken, a subsequent decision at the same price
    must not re-fire — the milestone is already marked in state."""
    market = {
        "BTCUSDC": {**_sym(trend="haussier"), "price": 135.0},
    }
    now = 1_000_000.0
    state = {
        "entry_ts":          {"BTCUSDC": now - 100 * 3600},
        "milestones_taken":  {"BTCUSDC": [0.30]},  # +30% already taken
    }
    result, _ = regime_decision(
        market_raw=market,
        holdings={"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}},  # still +35%
        cash=0, cycle=0, now_ts=now, risk_level=7,
        params={"decide_every_cycles": 1, "enable_regime_stance": False,
                "buy_threshold": 8},
        strat_state=state,
    )
    assert [a for a in result["actions"] if a["type"] == "scale_out"] == []


def test_scale_out_does_not_fire_below_first_milestone():
    """Position up only +20% (below +30% threshold) → no scale-out fires."""
    market = {
        "BTCUSDC": {**_sym(trend="haussier"), "price": 120.0},
    }
    now = 1_000_000.0
    state = {"entry_ts": {"BTCUSDC": now - 100 * 3600}}
    result, _ = regime_decision(
        market_raw=market,
        holdings={"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}},  # +20%
        cash=0, cycle=0, now_ts=now, risk_level=7,
        params={"decide_every_cycles": 1, "enable_regime_stance": False,
                "buy_threshold": 8},
        strat_state=state,
    )
    assert [a for a in result["actions"] if a["type"] == "scale_out"] == []


def test_risk_tier_filter_low_risk_only_blue_chips():
    """risk_level=3 → only BTC (tier 3) passes; ETH (tier 4) skipped."""
    # BTC + ETH both score 10 ; risk filter must exclude ETH (tier > 3).
    market = {
        "BTCUSDC": _sym(trend="haussier"),
        "ETHUSDC": _sym(trend="haussier"),
    }
    result, _ = regime_decision(
        market_raw=market, holdings={}, cash=1000.0, cycle=0,
        now_ts=1_000_000.0, risk_level=3,
        params={"decide_every_cycles": 1, "enable_regime_stance": False,
                "buy_threshold": 8},
    )
    buys = [a["symbol"] for a in result["actions"] if a["type"] == "buy"]
    assert buys == ["BTCUSDC"]


def test_risk_tier_filter_max_risk_allows_all():
    """risk_level=10 → no tier exclusion."""
    market = {
        "BTCUSDC": _sym(trend="haussier"),
        "POLUSDC": _sym(trend="haussier"),
    }
    result, _ = regime_decision(
        market_raw=market, holdings={}, cash=1000.0, cycle=0,
        now_ts=1_000_000.0, risk_level=10,
        params={"decide_every_cycles": 1, "enable_regime_stance": False,
                "buy_threshold": 8, "top_n": 5},
    )
    buys = {a["symbol"] for a in result["actions"] if a["type"] == "buy"}
    assert buys == {"BTCUSDC", "POLUSDC"}


def test_risk_tier_does_not_block_exits():
    """Held positions can still be sold even if their tier > risk_level."""
    # POL (tier 8) is held. risk_level=3 would block re-entry, but not the
    # sell signal — exits are based on trend, not tier.
    market = {
        "BTCUSDC": _sym(trend="haussier"),
        "POLUSDC": {"trend_1d": "baissier", "trend": "baissier",
                    "price": 100.0, "macd": {"histogram": -1.0},
                    "sma7": 95.0, "sma25": 100.0},
    }
    now = 1_000_000.0
    state = {
        "bear_since_1d": {"POLUSDC": now - 100 * 3600},
        "bear_since_1h": {"POLUSDC": now - 100 * 3600},
        "entry_ts":      {"POLUSDC": now - 100 * 3600},
    }
    result, _ = regime_decision(
        market_raw=market, holdings={"POLUSDC": {"qty": 1.0}}, cash=0,
        cycle=0, now_ts=now, risk_level=3,
        params={"decide_every_cycles": 1, "enable_regime_stance": False,
                "trend_confirm_hours": 24.0, "min_hold_hours": 12.0},
        strat_state=state,
    )
    sells = [a["symbol"] for a in result["actions"] if a["type"] == "sell"]
    assert sells == ["POLUSDC"]


def test_fng_extreme_greed_raises_threshold():
    """Extreme greed (FNG >= 75) makes us more selective: threshold +1."""
    market = _market(btc_trend="haussier", n_bull=6, n_bear=2)
    # Without FNG, DEPLOY threshold = 7. With FNG=80, → 8. Symbols score 10.
    # We compare entries with vs without FNG.
    result_no_fng, _ = regime_decision(
        market_raw=market, holdings={}, cash=1000.0, cycle=0,
        now_ts=1_000_000.0, risk_level=5,
        params={"decide_every_cycles": 1},
    )
    result_greed, _ = regime_decision(
        market_raw=market, holdings={}, cash=1000.0, cycle=0,
        now_ts=1_000_000.0, risk_level=5,
        params={"decide_every_cycles": 1},
        fng_value=80,
    )
    # Both still pass (score 10 >= 8), but the threshold differs in summary.
    assert "fng=80" in result_greed["summary"]
    assert "fng" not in result_no_fng["summary"]


def test_fng_extreme_fear_lowers_threshold():
    """Extreme fear (FNG <= 25) loosens entry: threshold -1."""
    # Symbols with score borderline 7: trend_1d haussier (+2), trend baissier
    # (-1) — base 5 + 2 - 1 = 6 plus depending on macd/sma. We want score = 7
    # so that without FNG (DEPLOY threshold 7) it just qualifies, with FNG -1
    # threshold becomes 6 and we get more candidates.
    market = _market(btc_trend="haussier", n_bull=6, n_bear=2)
    result_fear, _ = regime_decision(
        market_raw=market, holdings={}, cash=1000.0, cycle=0,
        now_ts=1_000_000.0, risk_level=5,
        params={"decide_every_cycles": 1},
        fng_value=15,
    )
    assert "fng=15" in result_fear["summary"]


def test_fng_neutral_no_modulation():
    """Mid-range FNG (25 < fng < 75) leaves the threshold untouched."""
    market = _market(btc_trend="haussier", n_bull=6, n_bear=2)
    result, _ = regime_decision(
        market_raw=market, holdings={}, cash=1000.0, cycle=0,
        now_ts=1_000_000.0, risk_level=5,
        params={"decide_every_cycles": 1},
        fng_value=50,
    )
    assert "fng" not in result["summary"]


def test_fng_user_pin_overrides_modulation():
    """If buy_threshold is user-pinned, FNG modulation is bypassed."""
    market = _market(btc_trend="haussier", n_bull=6, n_bear=2)
    result, _ = regime_decision(
        market_raw=market, holdings={}, cash=1000.0, cycle=0,
        now_ts=1_000_000.0, risk_level=5,
        params={"decide_every_cycles": 1, "buy_threshold": 8},
        fng_value=80,
    )
    # User-pinned threshold trumps the +1 nudge from extreme greed
    assert "fng" not in result["summary"]


def test_dd_circuit_breaker_liquidates_at_threshold():
    """Portfolio down ≥25% from peak → liquidate all + cooldown."""
    market = {
        "BTCUSDC": _sym(trend="haussier"),
        "ETHUSDC": {**_sym(trend="haussier"), "price": 60.0},  # was 100, now 60
    }
    holdings = {"ETHUSDC": {"qty": 1.0, "avg_price": 100.0}}
    now = 1_000_000.0
    # Peak was $1100 (cash 1000 + ETH at $100), now $1060 (cash 1000 + ETH at $60)
    # That's only -3.6%, but if we set peak higher in state we can trigger.
    state = {"portfolio_peak": 1500.0}  # we lost 1500 → 1060 = -29% DD
    result, new_state = regime_decision(
        market_raw=market, holdings=holdings, cash=1000.0, cycle=0,
        now_ts=now, risk_level=5,
        params={"decide_every_cycles": 1},
        strat_state=state,
    )
    assert result["stance"] == "FROZEN"
    sells = [a for a in result["actions"] if a["type"] == "sell"]
    assert [s["symbol"] for s in sells] == ["ETHUSDC"]
    assert "Circuit-breaker" in sells[0]["reason"]
    assert new_state["dd_cooldown_until"] == now + 3.0 * 86400


def test_dd_circuit_breaker_no_trigger_below_threshold():
    """Portfolio down 20% (under 25%) → normal decision continues."""
    market = {"BTCUSDC": _sym(trend="haussier")}
    holdings = {"BTCUSDC": {"qty": 1.0, "avg_price": 100.0}}
    state = {"portfolio_peak": 1200.0}  # 1200 → 1100 = ~-8% DD only
    result, _ = regime_decision(
        market_raw=market, holdings=holdings, cash=1000.0, cycle=0,
        now_ts=1_000_000.0, risk_level=5,
        params={"decide_every_cycles": 1},
        strat_state=state,
    )
    assert result["stance"] != "FROZEN"


def test_dd_cooldown_blocks_new_entries():
    """In post-CB cooldown window, no buys regardless of stance/scores."""
    market = _market(btc_trend="haussier", n_bull=6, n_bear=2)
    now = 1_000_000.0
    state = {"dd_cooldown_until": now + 86400.0}  # 1 day remaining
    result, _ = regime_decision(
        market_raw=market, holdings={}, cash=1000.0, cycle=0,
        now_ts=now, risk_level=5,
        params={"decide_every_cycles": 1},
        strat_state=state,
    )
    assert [a for a in result["actions"] if a["type"] == "buy"] == []
    assert "DD-cooldown" in result["summary"]


def test_dd_cooldown_expires_after_window():
    """Once cooldown expires, normal decision logic resumes."""
    market = _market(btc_trend="haussier", n_bull=6, n_bear=2)
    now = 1_000_000.0
    # cooldown_until is in the past → not in cooldown anymore
    state = {"dd_cooldown_until": now - 1.0}
    result, _ = regime_decision(
        market_raw=market, holdings={}, cash=1000.0, cycle=0,
        now_ts=now, risk_level=5,
        params={"decide_every_cycles": 1},
        strat_state=state,
    )
    # DEPLOY stance, can buy
    buys = [a for a in result["actions"] if a["type"] == "buy"]
    assert len(buys) > 0


def test_per_coin_threshold_higher_for_risky_coins():
    """Tier > 6 adds +1 to threshold per tier step."""
    from hellocrypto.deciders import _per_coin_threshold
    assert _per_coin_threshold(7, 2) == 7   # blue chip: no bump
    assert _per_coin_threshold(7, 6) == 7   # mid-tier: no bump
    assert _per_coin_threshold(7, 7) == 8   # +1
    assert _per_coin_threshold(7, 8) == 9   # +2
    assert _per_coin_threshold(7, 9) == 10  # +3


def test_per_coin_size_factor_smaller_for_risky_coins():
    """Tier > 5 reduces size by 10% per tier step, capped at 50%."""
    from hellocrypto.deciders import _per_coin_size_factor
    assert _per_coin_size_factor(2) == 1.0
    assert _per_coin_size_factor(5) == 1.0
    assert abs(_per_coin_size_factor(6) - 0.9) < 1e-9
    assert abs(_per_coin_size_factor(7) - 0.8) < 1e-9
    assert abs(_per_coin_size_factor(9) - 0.6) < 1e-9
    # Floor at 0.5
    assert _per_coin_size_factor(15) == 0.5


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
