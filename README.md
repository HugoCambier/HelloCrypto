# HelloCrypto

Agent de trading crypto autonome sur Binance, piloté par Claude (Anthropic).

## Structure

```
HelloCrypto/
├── hellocrypto/
│   ├── __init__.py      # package metadata
│   ├── api.py           # Binance REST client (auth, orders, historique)
│   ├── agent.py         # boucle de trading autonome
│   ├── dashboard.py     # serveur Flask (web UI)
│   └── prompts.py       # prompts Claude (système + analyse marché)
├── templates/
│   └── index.html       # interface web (logs, performance, portefeuille)
├── .env.example         # template des variables d'environnement
├── config.json          # paramètres (budget, stop-loss, watchlist)
├── Makefile             # commandes de développement
├── pyproject.toml       # dépendances Poetry
└── README.md
```

## Installation

```bash
# 1. Installer Poetry (si absent)
curl -sSL https://install.python-poetry.org | python3 -

# 2. Installer les dépendances dans un venv isolé
make install

# 3. Configurer les clés API
cp .env.example .env
# → éditer .env avec vos clés Binance et Anthropic
```

### Clés Binance
Compte Binance → **Gestion des API** → Créer une clé.
Permissions requises : **Lecture** + **Spot Trading** (jamais de retrait).

## Lancement

```bash
make dashboard   # Dashboard web → http://localhost:5000
make agent       # Agent seul (sans interface)
make shell       # Activer le shell Poetry pour des commandes manuelles
```

## Configuration — `config.json`

| Clé | Défaut | Description |
|-----|--------|-------------|
| `budget` | `100` | Budget total en USDC |
| `stop_loss_pct` | `10` | Seuil de stop-loss (%) |
| `cycle_seconds` | `60` | Intervalle entre chaque analyse |
| `watchlist` | `[BTCUSDC, …]` | Paires suivies |

## Dashboard

| Onglet | Contenu |
|--------|---------|
| **Logs en direct** | Flux SSE coloré, auto-scroll |
| **Performance** | Sélecteur de période, KPIs, frais, P&L net, historique |
| **Portefeuille** | Valeur totale, gain depuis le début, frais cumulés, cours live, achats/ventes manuels |

## Comportement de l'agent

- Analyse le marché toutes les 60 s (configurable)
- Claude (`claude-opus-4-5`) décide **buy / sell / hold** pour chaque actif
- Stop-loss automatique déclenché à −10 %
- Frais Binance capturés et inclus dans le P&L (USDC, BNB ou base asset)
- Rapport de performance loggé toutes les 10 cycles
