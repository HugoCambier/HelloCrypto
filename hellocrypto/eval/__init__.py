"""Evaluation harness for the trading strategy.

Replay historical or synthetic market scenarios to compare strategy versions
on the same data (alpha vs BTC B&H, sharpe, drawdown, tokens consumed).

Layout:
- scenario.py  — JSON scenario format (frozen market data per cycle)
- llm_cache.py — content-addressed file cache of LLM decisions
- runner.py    — replay engine, deterministic given the same scenario+config
- metrics.py   — pure metrics functions (alpha, sharpe, drawdown, etc.)
"""
