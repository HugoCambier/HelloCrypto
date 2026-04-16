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
│   ├── dashboard.py     # Factory Flask + enregistrement des blueprints
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
│   ├── store.py         # Persistance des trades et positions (Firestore / JSON local)
│   └── clean.py         # Utilitaires de nettoyage des données
├── runner/
│   └── main.py          # Point d'entrée commun (agent / simulation)
├── templates/
│   └── index.html       # Interface web
├── static/js/
│   └── main.js          # Logique frontend (Chart.js, fetch, SSE)
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
| Gemini (Google) | gemini-3.1-flash-lite-preview, gemini-3-flash-live |
| Ollama (local) | mistral, qwen2.5:14b, deepseek-r1, … |

Le provider et le modèle sont configurables à chaud depuis le dashboard sans redémarrage.
