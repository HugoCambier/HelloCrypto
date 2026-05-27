.PHONY: install hooks agent dashboard simulation shell deploy clean backtest bench bench-full bench-scenarios bench-ollama bench-ollama-full bench-ollama-overnight bench-progress propose

install:     ## Install dependencies (Gemini + PostgreSQL)
	poetry install --extras gemini --extras postgres

hooks:       ## Install local git hooks (regenerates requirements.txt on poetry changes)
	git config core.hooksPath scripts/git-hooks
	@echo "Hooks installed (core.hooksPath = scripts/git-hooks)"

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

bench:       ## A/B bench learning system vs baseline (compact 1d scenarios, throttled 12 RPM)
	LLM_RATE_LIMIT_RPM=12 poetry run python -m hellocrypto.eval.bench \
		--provider gemini --model gemini-3.1-flash-lite \
		--temperature 0.0 --min-confidence 0.5

bench-full:  ## Same as bench but on 7d scenarios (~1500 calls, runs ~2h with throttling)
	LLM_RATE_LIMIT_RPM=12 poetry run python -m hellocrypto.eval.bench \
		--scenarios "eval/scenarios/holdout/full/*.json" \
		--provider gemini --model gemini-3.1-flash-lite \
		--temperature 0.0 --min-confidence 0.5

bench-scenarios:  ## (Re)build the holdout scenarios from price_snapshots
	poetry run python -m scripts.build_holdout_scenarios --suite both

bench-progress:  ## Show live progress + ETA of a running bench
	@poetry run python -m hellocrypto.eval.progress

bench-ollama:      ## Bench compact via Ollama (caffeinate empêche le sleep). WORKERS=3 + OLLAMA_NUM_PARALLEL=4 pour ~3x speedup
	caffeinate -i poetry run python -m hellocrypto.eval.bench \
		--provider ollama --model $${OLLAMA_MODEL:-qwen2.5:14b} \
		--temperature 0.0 --min-confidence 0.5 \
		--workers $${WORKERS:-1}

bench-ollama-full: ## Bench 7j × 3 régimes × 5 variantes. WORKERS=3 recommandé
	poetry run python -m hellocrypto.eval.bench \
		--scenarios "eval/scenarios/holdout/full/*.json" \
		--provider ollama --model $${OLLAMA_MODEL:-qwen2.5:14b} \
		--temperature 0.0 --min-confidence 0.5 \
		--workers $${WORKERS:-1}

bench-ollama-overnight: ## Bench 7j + caffeinate (empêche le sleep macOS). WORKERS=3 OLLAMA_MODEL=qwen2.5:14b conseillés
	caffeinate -i poetry run python -m hellocrypto.eval.bench \
		--scenarios "eval/scenarios/holdout/full/*.json" \
		--provider ollama --model $${OLLAMA_MODEL:-qwen2.5:14b} \
		--temperature 0.0 --min-confidence 0.5 \
		--workers $${WORKERS:-3}

propose:     ## Proposer-agent: search params on TRAIN, gate winner on HOLDOUT, write report (rules decider, free). ARGS="--num-candidates 20 --seed 7"
	poetry run python -m scripts.propose $(ARGS)

clean:       ## Remove Python cache files
	find . -type d -name __pycache__ -exec rm -rf {} +
	find . -name "*.pyc" -delete
