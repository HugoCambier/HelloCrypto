.PHONY: install agent dashboard simulation shell deploy clean backtest

install:     ## Install dependencies (Gemini + PostgreSQL)
	poetry install --extras gemini --extras postgres

agent:       ## Run the trading agent locally (loops)
	RUNNER_LOOP=true poetry run python runner/main.py --mode real

simulation:  ## Run paper trading locally (loops)
	RUNNER_LOOP=true poetry run python runner/main.py --mode simulation

dashboard:   ## Start the web dashboard  →  http://localhost:5000
	poetry run dashboard

shell:       ## Activate the Poetry virtual environment
	poetry shell

deploy:      ## (obsolete — voir vercel.json + .github/workflows/runner.yml)
	@echo "Deploy via Vercel + GitHub Actions. Voir README."

backtest:    ## Run the backtester (pass args via ARGS="--days 30 --budget 1000")
	poetry run backtest $(ARGS)

clean:       ## Remove Python cache files
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -name "*.pyc" -delete
