"""Flask web dashboard — factory + blueprint registration."""

import logging
from dotenv import load_dotenv
from flask import Flask, render_template
from pathlib import Path

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
        bp_logs, bp_performance, bp_portfolio,
        bp_simulation, bp_backtest, bp_agent,
        bp_config, bp_analysis,
    )
    for bp in (
        bp_logs, bp_performance, bp_portfolio,
        bp_simulation, bp_backtest, bp_agent,
        bp_config, bp_analysis,
    ):
        app.register_blueprint(bp)

    @app.get("/")
    def index():
        return render_template("index.html")

    return app


app = create_app()


def main() -> None:
    (_ROOT / "logs").mkdir(exist_ok=True)
    (_ROOT / "data").mkdir(exist_ok=True)
    print("Dashboard → http://localhost:5000")
    app.run(host="0.0.0.0", port=5000, debug=False, threaded=True)


if __name__ == "__main__":
    main()
