"""Flask Blueprints — one per functional domain."""
from .logs        import bp as bp_logs
from .performance import bp as bp_performance
from .portfolio   import bp as bp_portfolio
from .simulation  import bp as bp_simulation
from .backtest    import bp as bp_backtest
from .agent       import bp as bp_agent
from .config      import bp as bp_config
from .analysis    import bp as bp_analysis

__all__ = [
    "bp_logs", "bp_performance", "bp_portfolio",
    "bp_simulation", "bp_backtest", "bp_agent",
    "bp_config", "bp_analysis",
]
