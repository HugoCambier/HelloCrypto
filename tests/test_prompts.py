"""Tests for prompt building — guards against silent regressions on the
output schema and length budget."""
from __future__ import annotations

from hellocrypto.api import format_market_data_compact
from hellocrypto.prompts import SYSTEM, build_analysis

_SAMPLE_MARKET = {
    "BTCUSDC": {
        "price": 50_000.0, "change_pct_24h": 3.5, "change_pct_1h": 1.0,
        "rsi14": 45, "trend": "haussier", "trend_short": "haussier", "trend_1d": "haussier",
        "macd": {"macd": 0.5, "signal": 0.3, "histogram": 0.2},
        "bollinger": {"lower": 49_000, "middle": 49_500, "upper": 50_100, "width_pct": 2.0},
        "volume_usdc": 1.5e9, "range_pct_24h": 2.5,
    },
    "ETHUSDC": {
        "price": 3_000.0, "change_pct_24h": -1.0, "change_pct_1h": -0.2,
        "rsi14": 65, "trend": "neutre", "trend_short": "neutre", "trend_1d": "haussier",
        "macd": {"macd": -0.1, "signal": -0.05, "histogram": -0.05},
        "bollinger": {"lower": 2_950, "middle": 3_000, "upper": 3_050, "width_pct": 3.3},
        "volume_usdc": 6e8, "range_pct_24h": 1.8,
    },
}


def test_compact_format_includes_score_and_signals():
    out = format_market_data_compact(
        _SAMPLE_MARKET, ["BTCUSDC", "ETHUSDC"],
        scores={"BTCUSDC": 8, "ETHUSDC": 5},
    )
    assert "BTCUSDC" in out
    assert "ETHUSDC" in out
    assert "H/H/H" in out         # trend trio rendered
    assert "↑hi" in out or "mid-hi" in out  # BB position present
    assert "8" in out             # score is in the row


def test_compact_format_handles_missing_symbol():
    out = format_market_data_compact(_SAMPLE_MARKET, ["BTCUSDC", "DOGEUSDC"], scores={})
    assert "DOGEUSDC | n/a" in out


def test_compact_format_omits_macd_dash_when_absent():
    """If macd is None we render '—' rather than crash."""
    data = {"BTCUSDC": {**_SAMPLE_MARKET["BTCUSDC"], "macd": None}}
    out = format_market_data_compact(data, ["BTCUSDC"], scores={"BTCUSDC": 5})
    assert "—" in out


def test_system_prompt_mentions_confluence_and_json():
    assert "confluence" in SYSTEM.lower()
    assert "json" in SYSTEM.lower()


def test_build_analysis_includes_schema_and_confidence_field():
    prompt = build_analysis(
        market_data="BTCUSDC | $50,000 | +3% | 45 | H/H/H | + | mid-hi | $1B | 2% | 7",
        positions={}, cash=1000.0, budget=1000.0, risk_level=5,
        fear_greed={"value": 50, "label": "Neutral"}, btc_dominance=55.0,
        scores={"BTCUSDC": 7},
    )
    # schema baked into the prompt should reference all the new fields
    assert "confidence" in prompt
    assert "horizon" in prompt
    assert "market_sentiment" in prompt
    assert "reasoning" in prompt
    # No leftover verbose tutorial sections
    assert "GUIDE D'INTERPRÉTATION" not in prompt


def test_build_analysis_token_budget_under_2700_chars():
    """The lean prompt must stay well under the legacy verbose one (~3 k tokens).

    2700 chars ≈ ~680 tokens — a generous ceiling for a no-history,
    no-positions cycle with a 3-symbol compact table. Legacy prompt was
    ~7500+ chars on the same input.
    """
    md = format_market_data_compact(
        {**_SAMPLE_MARKET,
         "SOLUSDC": {"price": 150, "change_pct_24h": 0, "change_pct_1h": 0,
                     "rsi14": 50, "trend": "neutre", "trend_short": "neutre", "trend_1d": "neutre",
                     "macd": None, "bollinger": None, "volume_usdc": 1e8, "range_pct_24h": 3}},
        ["BTCUSDC", "ETHUSDC", "SOLUSDC"],
        scores={"BTCUSDC": 7, "ETHUSDC": 5, "SOLUSDC": 5},
    )
    prompt = build_analysis(
        market_data=md, positions={}, cash=1000.0, budget=1000.0, risk_level=5,
        fear_greed={"value": 50, "label": "Neutral"}, btc_dominance=55.0,
        scores={"BTCUSDC": 7, "ETHUSDC": 5, "SOLUSDC": 5},
    )
    assert len(prompt) < 2700, f"prompt trop long : {len(prompt)} chars"


def test_build_analysis_profile_switches_by_risk_level():
    common = dict(
        market_data="x", positions={}, cash=100, budget=100,
        fear_greed=None, btc_dominance=None, scores=None,
    )
    prudent  = build_analysis(risk_level=2, **common)
    modere   = build_analysis(risk_level=5, **common)
    agressif = build_analysis(risk_level=9, **common)
    assert "PRUDENT" in prudent
    assert "MODÉRÉ" in modere
    assert "AGRESSIF" in agressif
