"""Flask web dashboard — factory + blueprint registration."""

from __future__ import annotations

import logging
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

    # Gzip/Brotli all responses with Content-Type application/json (default
    # mime list also covers text/html, text/css, application/javascript).
    # @vercel/python doesn't auto-compress dynamic responses — without this,
    # 100 KB JSON payloads ship as 100 KB; with it, they ship as ~15-25 KB
    # over the wire, which is what counts toward the Supabase/Vercel egress.
    try:
        from flask_compress import Compress
        Compress(app)
    except ImportError:
        log.warning("flask-compress not installed — responses won't be gzipped")

    from .routes import (
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
        bp_simulation, bp_backtest,
        bp_config, bp_analysis, bp_cron,
    ):
        app.register_blueprint(bp)

    # Single source of truth for the coin universe AND the run defaults
    # (stop_loss, trailing, risk_level, budget…): config.json. Templates use
    # these to pre-fill form inputs so the user gets sensible starting values
    # consistent with what the live/sim runners use.
    @app.context_processor
    def _inject_template_context() -> dict:
        try:
            from .api import load_config
            cfg = load_config() or {}
            return {
                "coin_universe": cfg.get("watchlist", []) or [],
                "cfg_defaults": {
                    "budget":            cfg.get("budget", 1000),
                    "stop_loss_pct":     cfg.get("stop_loss_pct", 10),
                    "trailing_stop_pct": cfg.get("trailing_stop_pct", 5),
                    "risk_level":        cfg.get("risk_level", 5),
                },
            }
        except Exception:
            log.exception("Failed to load cfg for template context")
            return {"coin_universe": [],
                    "cfg_defaults": {"budget": 1000, "stop_loss_pct": 10,
                                     "trailing_stop_pct": 5, "risk_level": 5}}

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
