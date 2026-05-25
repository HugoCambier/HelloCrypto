"""Flask web dashboard — factory + blueprint registration."""

from __future__ import annotations

import logging
import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, render_template

log = logging.getLogger(__name__)

load_dotenv()

_ROOT = Path(__file__).parent.parent


def create_app() -> Flask:
    app = Flask(
        __name__,
        template_folder=str(_ROOT / "templates"),
        static_folder=str(_ROOT / "static"),
        static_url_path="/static",
    )

    from .routes import (
        bp_agent,
        bp_analysis,
        bp_backtest,
        bp_config,
        bp_cron,
        bp_logs,
        bp_performance,
        bp_portfolio,
        bp_simulation,
    )
    for bp in (
        bp_logs, bp_performance, bp_portfolio,
        bp_simulation, bp_backtest, bp_agent,
        bp_config, bp_analysis, bp_cron,
    ):
        app.register_blueprint(bp)

    # Single source of truth for the coin universe: the runtime watchlist
    # from config.json. Templates inject it as ``window.COIN_UNIVERSE`` so
    # no JS file needs a hardcoded list. Failures fall back to [] — the UI
    # then degrades gracefully (no symbols selectable) rather than crashing.
    @app.context_processor
    def _inject_coin_universe() -> dict:
        try:
            from .api import load_config
            return {"coin_universe": load_config().get("watchlist", [])}
        except Exception:
            log.exception("Failed to load watchlist for template context")
            return {"coin_universe": []}

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/backtest")
    def backtest_page():
        return render_template("backtest.html")

    @app.get("/market")
    def market_page():
        return render_template("market.html")

    return app


app = create_app()


def main() -> None:
    (_ROOT / "logs").mkdir(exist_ok=True)
    (_ROOT / "data").mkdir(exist_ok=True)
    port = int(os.environ.get("PORT", os.environ.get("FLASK_PORT", "5000")))
    print(f"Dashboard → http://localhost:{port}")
    app.run(host="0.0.0.0", port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
