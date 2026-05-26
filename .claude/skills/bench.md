---
name: bench
description: Run the A/B learning-system bench on held-out scenarios and interpret the result. Use when the user wants to measure whether a change to the prompt, strategy, learning system, or thresholds actually improves trading decisions.
---

# /bench — A/B benchmark the learning system

Mesure honnêtement si un changement (prompt, stratégie, calibration, seuils) améliore
les décisions de l'agent, sur 3 scénarios held-out couvrant des régimes contrastés
(fear+bear, neutral+bull, greed+bull). 5 variants incrémentales sont comparées :
baseline → playbook → full_prompt → calibrated → full_learning.

## When to invoke (proactive triggers)

Quand l'utilisateur :
- Termine une modif de `hellocrypto/prompts.py`, `strategy.py`, `eval/playbook.py`,
  `eval/behavior.py`, `eval/patterns.py`
- Change un seuil structurel (`min_confidence`, `risk_level`, watchlist)
- Bascule un flag `enable_playbook`/`enable_behavior`/`enable_confidence_calibration`/
  `enable_regime_aware_thresholds`
- Demande explicitement "lance le bench" / "mesure l'effet de ce changement" /
  "compare avec avant"

Ne PAS invoquer pour : refactor cosmétique, JS/CSS, doc, lint, bug fix non-stratégique.

## Execution flow

1. **Préflight checks** (rapide, avant de lancer):
   - Vérifier que `.env` a `GEMINI_API_KEY` (sinon le bench échoue immédiatement)
   - Vérifier que `eval/scenarios/holdout/compact/` contient bien 3 fichiers
     (sinon : `make bench-scenarios` pour les régénérer)
   - Vérifier que le playbook + behavior sont à jour en DB :
     `poetry run python -c "from db.store import get_state; print('pb' if get_state('playbook') else 'NO PB'); print('bh' if get_state('behavior') else 'NO BH')"`
     S'il manque l'un des deux, lancer `_maybe_rebuild_playbook` et `_maybe_rebuild_behavior` via le cron endpoint avant de bencher.

2. **Lancer le bench compact en arrière-plan** :
   ```
   make bench
   ```
   Durée attendue : ~10-15 min (24 cycles × 3 scénarios × 5 variants, throttle 12 RPM,
   LLM cache aide après le 1er run). Lancer avec `run_in_background=true` Bash tool.

3. **Pendant que ça tourne** : continuer à répondre aux autres demandes user (ne pas
   bloquer la conversation).

4. **À la complétion** : lire le rapport JSON dans `eval/reports/bench/bench_*.json`
   et présenter à l'utilisateur :
   - Le verdict global (SHIP / MIXED / HOLD)
   - Le delta `full_learning - baseline` sur les métriques clés
     (return %, alpha vs BTC %, max DD %, Sharpe)
   - Pour chaque scénario, un mini-bilan en 1 ligne (mieux / pareil / pire)
   - Si MIXED ou HOLD : identifier QUELLE couche a posé problème (playbook ? behavior ?
     calibration ? thresholds ?) en regardant les deltas entre variants successives

5. **Recommander une suite** :
   - SHIP → suggérer commit + push
   - MIXED → suggérer une investigation ciblée (quelle couche est neutre ou contre-prod?)
   - HOLD → suggérer un rollback ou un ajustement avant retest

## Important behaviors

- **Ne PAS lancer le bench complet (7d) sans confirmation explicite** — il consomme
  ~1500 calls Gemini, soit la quasi-totalité du quota free tier d'une journée.
  La commande est `make bench-full` mais à ne suggérer que si l'utilisateur veut
  un signal statistique plus solide après un bench compact convaincant.

- **Si le bench échoue sur 429 malgré le throttle** : pas grave, c'est récupérable.
  Le LLM cache préserve les appels déjà réussis. Re-lancer plus tard reprendra où
  ça s'est arrêté (cache hits sur les prompts déjà passés).

- **Si tous les variants montrent un delta nul** : c'est suspect. Vérifier que
  `--provider gemini` est bien utilisé (avec `--provider rules`, les règles ignorent
  le prompt donc tous les variants sont identiques — normal mais inutile pour la
  mesure).

- **Le rapport JSON est conservé** dans `eval/reports/bench/` même après
  fermeture du terminal. L'utilisateur peut y revenir.

## Cost notes

- Premier bench compact : ~150 appels Gemini Flash Lite ≈ $0.05, durée ~10-15 min
- Re-bench après refactor non-prompt : ~0 appel (cache 100%), durée ~30s
- Re-bench après changement de prompt : 30-50% miss cache, durée ~5-10 min
- Free tier Gemini : 1500 RPD → ~10 benchs compacts/jour max
