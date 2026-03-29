.PHONY: install install-gemini agent dashboard shell clean gemini-models backtest

install:         ## Install core dependencies (Claude only)
	poetry install

install-gemini:  ## Install with Gemini support
	poetry install --extras gemini

agent:      ## Run the autonomous trading agent
	poetry run agent

dashboard:  ## Start the web dashboard  →  http://localhost:5000
	poetry run dashboard

shell:      ## Activate the Poetry virtual environment
	poetry shell

gemini-models: ## List available Gemini models for your API key
	poetry run python -c "from dotenv import load_dotenv; load_dotenv()" && poetry run python get_google_llm_versions.py

backtest:   ## Run the rule-based backtester  (pass args via ARGS="--days 30 --budget 1000")
	poetry run backtest $(ARGS)

clean:      ## Remove Python cache files
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -name "*.pyc" -delete
