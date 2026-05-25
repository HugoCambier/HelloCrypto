# HelloCrypto

Agent de trading crypto autonome sur Binance, piloté par LLM (Claude, Gemini, Ollama).

## Structure

```
HelloCrypto/
├── hellocrypto/
│   ├── agent.py         # Boucle de trading autonome (mode réel)
│   ├── simulation.py    # Paper trading (mode simulation, sans ordres réels)
│   ├── backtest.py      # Backtester historique (rule-based ou LLM)
│   ├── trading.py       # Primitives partagées : frais, stop-loss, sizing, take-profit
│   ├── api.py           # Client Binance REST + indicateurs techniques (MACD, BB, ATR)
│   ├── llm.py           # Abstraction multi-provider (Claude / Gemini / Ollama)
│   ├── prompts.py       # Tous les prompts LLM centralisés
│   ├── strategy.py      # Helpers décision/exécution (peaks, cooldowns, stops)
│   ├── cron.py          # Tick cron : rebuild playbook + behavior + purge logs
│   ├── dashboard.py     # Factory Flask + enregistrement des blueprints
│   ├── eval/            # Système d'apprentissage continu (voir section dédiée)
│   │   ├── patterns.py     # 10 setups nommés (oversold_reversal, falling_knife, …)
│   │   ├── journal.py      # Horizon returns + MAE/MFE par pattern × régime
│   │   ├── playbook.py     # Distille le journal → leçons favored/avoid par régime
│   │   ├── behavior.py     # Comportement passé : exécutés + occasions ratées + calibration
│   │   ├── capture.py      # Persiste un snapshot live à chaque cycle
│   │   └── scenario.py, runner.py, metrics.py, llm_cache.py   # Replay engine
│   └── routes/
│       ├── agent.py        # /api/agent/* — cycle de vie de l'agent
│       ├── simulation.py   # /api/simulation/* — paper trading
│       ├── backtest.py     # /api/backtest/* — backtest + grid search
│       ├── analysis.py     # /api/analysis/* — analyse de marché IA
│       ├── performance.py  # /api/performance + /api/watchlist/*
│       ├── portfolio.py    # /api/portfolio + ordres manuels
│       ├── config.py       # /api/config/llm + /api/ollama/*
│       └── logs.py         # /api/logs/* — SSE + historique base de données
├── db/
│   ├── store.py         # Persistance trades / sessions / analyses (SQLite / PostgreSQL / Firestore)
│   ├── snapshots.py     # Table price_snapshots (OHLCV + indicateurs + régime)
│   └── clean.py         # Utilitaires de nettoyage des données
├── runner/
│   └── main.py          # Point d'entrée commun (agent / simulation)
├── scripts/
│   ├── backfill_binance.py  # Bootstrap 12mo d'historique depuis Binance
│   ├── snapshot_scenario.py # Capture un scénario figé pour replay
│   └── eval.py, compare.py  # Outils d'évaluation/comparaison
├── templates/           # index.html, backtest.html, market.html
├── static/js/           # main.js, backtest.js, market.js, analytics.js, orders.js
├── .env.example         # Template des variables d'environnement
├── config.json          # Paramètres de trading
├── Makefile             # Commandes de développement
└── pyproject.toml       # Dépendances Poetry
```

## Installation

```bash
# 1. Installer Poetry (si absent)
curl -sSL https://install.python-poetry.org | python3 -

# 2. Installer les dépendances (avec support Gemini)
make install

# 3. Configurer les clés API
cp .env.example .env
# → éditer .env avec vos clés Binance, Anthropic, Gemini
```

### Clés requises (`.env`)

| Variable | Description |
|----------|-------------|
| `BINANCE_API_KEY` / `BINANCE_API_SECRET` | Clés Binance — permissions : Lecture + Spot Trading (jamais de retrait) |
| `ANTHROPIC_API_KEY` | Pour le provider Claude |
| `GEMINI_API_KEY` | Pour le provider Gemini |

## Lancement

```bash
make dashboard    # Dashboard web → http://localhost:5000
make agent        # Agent réel seul (sans interface)
make simulation   # Paper trading seul (sans interface)
make backtest ARGS="--days 30 --budget 1000"  # Backtester en ligne de commande
make shell        # Activer le shell Poetry
make deploy       # Déployer sur GCP (Cloud Run + Firestore + Scheduler)
```

## Configuration — `config.json`

| Clé | Défaut | Description |
|-----|--------|-------------|
| `budget` | `100` | Budget total en USDC |
| `watchlist` | `[BTCUSDC, …]` | Paires suivies |
| `cycle_seconds` | `3600` | Intervalle entre chaque analyse |
| `risk_level` | `5` | Niveau de risque 1–10 (profil PRUDENT / MODÉRÉ / AGRESSIF) |
| `stop_loss_pct` | `21` | Seuil de stop-loss fixe (%) |
| `trailing_stop_pct` | `10` | Trailing stop depuis le pic (%) |
| `sell_cooldown_cycles` | `3` | Nombre de cycles minimum entre deux ventes du même actif |
| `llm_cooldown_seconds` | `300` | Délai minimum entre deux appels LLM |
| `price_change_threshold_pct` | `0.5` | Variation de prix minimale pour déclencher une analyse LLM |
| `llm.provider` | `gemini` | Provider actif : `claude`, `gemini`, `ollama` |
| `llm.model` | `gemini-…` | Modèle à utiliser (configurable depuis le dashboard) |

## Dashboard

L'interface est organisée en **4 onglets** + un **tiroir de logs** latéral.

| Onglet | Contenu |
|--------|---------|
| **Dashboard** | KPIs temps réel (cash, P&L, win rate, frais), positions ouvertes, derniers trades, simulation live |
| **Marchés** | Watchlist enrichie (prix, Δ24h, RSI, tendance, score, volume), ordres manuels, analyse IA par actif |
| **Performance** | Sélecteur mode (réel / simulation), courbe de capital, P&L par symbole, historique des trades, sessions |
| **Backtest** | Configuration et lancement de backtests, visualisation des résultats, grid search de paramètres |

Le bouton **Logs** (barre de navigation) ouvre un tiroir latéral avec le flux SSE en temps réel.

## Fonctionnalités

### Trading
- Décisions **buy / sell / hold** par le LLM à chaque cycle d'analyse
- **Stop-loss fixe** et **trailing stop** automatiques
- **Take-profit multi-niveaux** (vente partielle par palier)
- **Timeout de position** : fermeture forcée des positions stagnantes
- Sizing dynamique basé sur le `risk_level` et le RSI (±50 %)

### Indicateurs techniques
- RSI (1h et court terme), SMA 7/25, tendance
- **MACD** (12, 26, 9), **Bandes de Bollinger** (20 périodes, 2σ), **ATR** (14)
- Score de signal composite 0–10, intégré au prompt LLM

### Analyse de marché IA
- Analyse complète multi-actifs (provider cloud) ou actif par actif (Ollama local)
- Scénarios bear / base / bull avec probabilités et projections de prix (24h, 7j, 30j)
- Contexte Fear & Greed et dominance BTC automatiquement injectés

### Backtest
- Mode **rule-based** (RSI/SMA/volatilité) ou **LLM-driven**
- **Grid search** : balayage cartésien de (risk_level × stop_loss × trailing_stop), résultats triés par P&L
- Vitesse de simulation réglable en temps réel (1×–500×)

### Providers LLM supportés
| Provider | Modèles |
|----------|---------|
| Claude (Anthropic) | claude-sonnet-4-6, claude-opus-4-5, claude-haiku-4-5 |
| Gemini (Google) | gemini-3.1-flash-lite, gemini-3.5-flash, gemini-2.5-flash-lite |
| Ollama (local) | mistral, qwen2.5:14b, deepseek-r1, … |

Le provider et le modèle sont configurables à chaud depuis le dashboard sans redémarrage.

## Système d'apprentissage continu

L'agent n'évalue pas chaque cycle de manière indépendante : trois couches de
feedback bâties sur l'historique sont injectées dans le prompt à chaque décision.

| Couche | Source | Question répondue |
|---|---|---|
| **Macro** | F&G + trend BTC daily | Dans quel régime suis-je ? |
| **Playbook** | `price_snapshots` (12+ mois) | Quels patterns sont rentables dans ce régime, net de frais ? |
| **Behavior** | `trades` + `market_analyses` joints aux snapshots | Mon track record dans ce régime : trades exécutés, occasions ratées, calibration confidence |

**Régimes** : F&G bucket (`fear` / `neutral` / `greed`) × trend BTC daily (`bear` / `range` / `bull`) → 9 régimes max, fallback `general` sous échantillon faible.

**Bootstrap initial** (une fois après installation) :
```bash
poetry run python -m scripts.backfill_binance --days 365
```
Charge 12 mois d'OHLCV horaires de toute la watchlist + F&G historique, calcule les indicateurs (RSI, MACD, Bollinger, ATR, SMA, trend), pré-tague le régime, et persiste dans `price_snapshots`. ~25 Mo / 87k lignes pour une watchlist de 10 symboles.

**Cycle de vie automatique** — le cron tick (ping `/api/cron/tick` toutes les 5 min) déclenche :

| Tâche | Cadence | Sentinel |
|---|---|---|
| Régénération playbook | 24 h | `agent_state.last_playbook_rebuild_at` |
| Régénération behavior | 6 h | `agent_state.last_behavior_rebuild_at` |
| Purge logs > 14 j | 24 h | `agent_state.last_log_purge_at` |

Chaque cycle agent / simulation ajoute un snapshot live dans `price_snapshots` (UPSERT intra-heure → aligné sur la grille horaire du backfill), donc la base d'apprentissage grossit continuellement et les rebuilds intègrent les nouvelles données.

**Inspection manuelle** :
```bash
# Régénérer le playbook à la demande (CLI directe)
poetry run python -m hellocrypto.eval.playbook --out data/playbook.json

# Voir le rapport behavior brut
poetry run python -m hellocrypto.eval.behavior --mode simulation
```
