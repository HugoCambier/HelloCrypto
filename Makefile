.PHONY: install agent dashboard simulation shell deploy clean backtest

install:     ## Install dependencies (with Gemini support)
	poetry install --extras gemini

agent:       ## Run the trading agent locally (loops)
	RUNNER_LOOP=true poetry run python runner/main.py --mode real

simulation:  ## Run paper trading locally (loops)
	RUNNER_LOOP=true poetry run python runner/main.py --mode simulation

dashboard:   ## Start the web dashboard  →  http://localhost:5000
	poetry run dashboard

shell:       ## Activate the Poetry virtual environment
	poetry shell

deploy:      ## Deploy to GCP (Cloud Run + Firestore + Scheduler)
	bash deploy/deploy.sh

backtest:    ## Run the backtester (pass args via ARGS="--days 30 --budget 1000")
	poetry run backtest $(ARGS)

clean:       ## Remove Python cache files
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -name "*.pyc" -delete
