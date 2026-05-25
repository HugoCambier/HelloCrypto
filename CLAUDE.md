# HelloCrypto — Claude project rules

## Security

**Ne jamais installer une librairie publiée depuis moins de 24 heures.**

Avant tout `pip install` d'un nouveau paquet, vérifier sa date de première publication sur PyPI
(via `pip index versions <package>` ou `https://pypi.org/pypi/<package>/json`).
Si le paquet a été publié il y a moins de 1 jour, refuser l'installation et en informer l'utilisateur.

Cette règle vise à éviter les attaques par typosquatting et les compromissions de chaîne
d'approvisionnement (supply chain attacks) via des paquets malveillants fraîchement publiés.

## Système d'apprentissage — rappel bench

Le projet a un système d'apprentissage à 3 couches (playbook + behavior + calibration)
qui s'injecte dans le prompt LLM. Pour mesurer honnêtement si un changement améliore
ou dégrade les décisions, il existe un bench A/B sur scénarios held-out :
`make bench` (ou `/bench` via Skill).

**Avant de marquer comme terminé un changement qui touche un de ces fichiers, rappeler
à l'utilisateur de lancer `make bench` pour mesurer l'impact :**

- `hellocrypto/prompts.py` (prompt LLM de décision)
- `hellocrypto/strategy.py` (sizing, gating, exécution paper)
- `hellocrypto/eval/playbook.py` ou `eval/behavior.py` (logique d'apprentissage)
- `hellocrypto/eval/patterns.py` (bibliothèque de setups)
- Changement structurel de `config.json` (seuils min_confidence, risk_level, watchlist…)
- Bascule d'un flag `enable_*` dans la stratégie

**Ne PAS le rappeler pour :**
- Refactor cosmétique, lint, docs, dashboard/JS, bug fixes non-stratégiques
- Modifs `cron.py`, `db/`, routes Flask hors décision

Le rappel doit être bref et factuel : *"Ce changement touche la logique de décision —
pense à `make bench` avant de pousser en prod."* Ne pas le lancer automatiquement
(coûte ~5-15 min + des appels LLM même throttlés).
