"""End-to-end tests for the eval replay engine (no network, no LLM)."""
from __future__ import annotations

from hellocrypto.eval.runner import StrategyConfig, run
from hellocrypto.eval.scenario import Cycle, Scenario


def _synthetic_scenario(name: str = "synthetic_btc_up") -> Scenario:
    """BTC linearly up, ETH flat, SOL down — over 12 cycles."""
    cycles: list[Cycle] = []
    btc_p, eth_p, sol_p = 50_000.0, 3_000.0, 150.0
    for i in range(12):
        # walk
        btc_p *= 1.01   # +1% each cycle → BTC gains ~12.7% over the window
        eth_p *= 1.00   # flat
        sol_p *= 0.99   # -1% each cycle → SOL loses ~11.4%
        cycles.append(Cycle(
            timestamp=f"2026-05-01T{i:02d}:00:00",
            market={
                "BTCUSDC": {"price": btc_p, "change_pct_24h": 3.0,
                            "rsi14": 35.0, "trend": "haussier", "trend_1d": "haussier",
                            "range_pct_24h": 2.0, "volume_usdc": 1e9,
                            "macd": None, "bollinger": None, "atr": None},
                "ETHUSDC": {"price": eth_p, "change_pct_24h": 0.1,
                            "rsi14": 50.0, "trend": "neutre", "trend_1d": "neutre",
                            "range_pct_24h": 1.5, "volume_usdc": 5e8,
                            "macd": None, "bollinger": None, "atr": None},
                "SOLUSDC": {"price": sol_p, "change_pct_24h": -2.5,
                            "rsi14": 80.0, "trend": "baissier", "trend_1d": "baissier",
                            "range_pct_24h": 5.0, "volume_usdc": 1e8,
                            "macd": None, "bollinger": None, "atr": None},
            },
            fear_greed={"value": 50, "label": "Neutral"},
            btc_dominance=55.0,
        ))
    return Scenario(
        name=name, start="2026-05-01T00:00:00", end="2026-05-01T12:00:00",
        watchlist=["BTCUSDC", "ETHUSDC", "SOLUSDC"],
        cycle_seconds=3600, cycles=cycles,
    )


def test_runner_returns_metrics_for_rule_based():
    cfg = StrategyConfig(budget=1000, risk_level=5, provider="rules",
                         buy_score_min=6, sell_score_max=4)
    report = run(_synthetic_scenario(), cfg, version="test_rules")
    assert report.cycles_run == 12
    # Doit avoir traité de l'historique
    assert isinstance(report.metrics["alpha_vs_btc_pct"], float)
    assert report.metrics["btc_return_pct"] > 0  # BTC monte sur le scénario


def test_runner_is_deterministic():
    cfg = StrategyConfig(budget=1000, risk_level=5, provider="rules")
    r1 = run(_synthetic_scenario(), cfg, version="det1")
    r2 = run(_synthetic_scenario(), cfg, version="det2")
    assert r1.metrics["return_pct"] == r2.metrics["return_pct"]
    assert r1.metrics["num_trades"] == r2.metrics["num_trades"]


def test_runner_rule_buys_high_score_assets():
    """Avec buy_score_min bas et BTC qui tend à scorer haut, on doit acheter BTC."""
    cfg = StrategyConfig(budget=1000, risk_level=5, provider="rules",
                         buy_score_min=6, sell_score_max=4)
    report = run(_synthetic_scenario(), cfg, version="buyer")
    bought_symbols = {t["symbol"] for t in report.trades if t["action"] == "BUY"}
    assert "BTCUSDC" in bought_symbols


def test_custom_decision_fn_overrides_provider():
    """Permettre d'injecter un decideur personnalisé (utile pour des tests ciblés)."""
    calls = []

    def always_hold(market, scores, holdings, cfg):
        calls.append(len(market))
        return ({"market_sentiment": "neutral", "summary": "test",
                 "actions": [{"type": "hold", "symbol": "BTCUSDC", "score": 5}]},
                {"in": 1, "out": 1})

    cfg = StrategyConfig(budget=1000, provider="anything")
    report = run(_synthetic_scenario(), cfg, version="custom",
                 decision_fn=always_hold)
    assert len(calls) == 12  # appelé une fois par cycle
    assert report.metrics["num_trades"] == 0  # aucun trade
    assert report.metrics["tokens_total"] == 24  # 12 * (1+1)
