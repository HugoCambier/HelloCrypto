"""Shared constants used across route blueprints."""
from datetime import timedelta
from pathlib import Path

# Project root (two levels up from this file: hellocrypto/routes/shared.py → /)
_ROOT = Path(__file__).parent.parent.parent

_LOG_FILE = _ROOT / "logs" / "agent.log"

PERIODS: dict[str, timedelta] = {
    "1h":  timedelta(hours=1),
    "6h":  timedelta(hours=6),
    "24h": timedelta(hours=24),
    "3j":  timedelta(days=3),
    "7j":  timedelta(days=7),
    "30j": timedelta(days=30),
    "all": timedelta(days=9999),
}
