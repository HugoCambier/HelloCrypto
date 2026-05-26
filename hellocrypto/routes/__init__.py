"""Flask Blueprints — one per functional domain."""
from .analysis import bp as bp_analysis
from .backtest import bp as bp_backtest
from .config import bp as bp_config
from .cron import bp as bp_cron
from .logs import bp as bp_logs
from .performance import bp as bp_performance
from .portfolio import bp as bp_portfolio
from .simulation import bp as bp_simulation

__all__ = [
    "bp_logs", "bp_performance", "bp_portfolio",
    "bp_simulation", "bp_backtest",
    "bp_config", "bp_analysis", "bp_cron",
]
