---
name: bench
description: Propose the right benchmark for a change that touches a decision mechanism, then run it on "oui" and interpret results. Picks between bench-path (deterministic 1000d replay), bench (LLM A/B held-out), bench-fast (smoke test), or bench-full (deep validation) based on what changed.
---

# /bench — Choisir et lancer le bon test pour une modif de décision

À chaque changement qui touche la logique de décision, propose **une** commande
appropriée + ETA + raison. L'utilisateur répond "oui" → tu lances. Réponse
autre → tu n'insistes pas.

## Contrainte free-tier Gemini

Gemini Flash Lite est utilisé en free-tier uniquement (jamais payant). Limites
dures : **15 RPM, 1500 RPD**. Les benchs LLM sont throttlés à 12 RPM pour
marge, et `bench-full` consomme à lui seul ~la moitié du quota journalier.
Privilégier les bench déterministes (sans LLM) quand le change ne touche pas
le prompt ou le système d'apprentissage.

## Décider quel bench proposer

| Fichier(s) touché(s) | Bench à proposer | Pourquoi | ETA |
|---|---|---|---|
| `hellocrypto/deciders.py`, `hellocrypto/strategy.py`, `hellocrypto/backtest.py` (scoring, seuils, sizing, gating, exit-logic) | `make bench-path` | Mesure path-dependence sur 1000j réels (médiane 4 cellules mod-4) — déterministe, **0 appel LLM**, captures interactions de marché long-terme | ~15 min |
| `config.json` (seuils, watchlist, risk_level structurel) | `make bench-path` | Idem — change déterministe sur l'exécution | ~15 min |
| `hellocrypto/prompts.py` (prompt LLM de décision) | `make bench` | A/B held-out avec LLM, mesure si le prompt change les décisions | ~10-15 min |
| `hellocrypto/eval/playbook.py`, `behavior.py`, `patterns.py` | `make bench` | Le système d'apprentissage 3-couches s'évalue sur scenarios held-out | ~10-15 min |
| Bascule d'un flag `enable_*` (regime stance, playbook, calibration…) | `make bench-path` *ou* `make bench` selon que le flag pilote la rule-based ou le prompt | — | ~15 min |
| Petite refactor de logique rule-based, smoke test rapide | `make bench-fast` | Sanity check sur scenarios courts, **0 appel LLM** | ~1 min |
| Validation rigoureuse demandée *explicitement* après un changement majeur | `make bench-full` | 7d scenarios, **consomme ~la moitié du quota journalier Gemini** | ~2h |

**Ne PAS proposer de bench** pour : refactor cosmétique, lint, doc, dashboard
JS/CSS, fix non-stratégique, modifs `cron.py`/`db/`/routes Flask hors décision.

## Format de proposition

Après avoir conclu une modif qui mérite un bench, écris **une seule ligne**
suivie d'une question oui/non :

```
Ce changement touche [X] (ex: scoring du décideur).
Je propose `make bench-path` (~15 min, 0 appel LLM) — il mesure la médiane
des 4 cellules mod-4 sur 1000 jours réels, qui est la métrique honnête pour
ce type de change.

Lancer ? (oui / non / autre)
```

- Sois explicite sur **quelle métrique** sera comparée à quoi (typiquement le
  baseline noté dans `BACKTEST_LOG.md`).
- Mentionne **0 appel LLM** si c'est un bench déterministe — rassure sur
  l'absence de consommation du quota.
- Donne l'ETA en clair, pas en jargon.
- Si l'utilisateur a déjà un baseline mesuré récent, le mentionner pour
  cadrer la comparaison.

## Exécution sur "oui"

Lance la commande **en background** (`run_in_background=true` dans Bash) et
continue à répondre aux autres demandes pendant que ça tourne.

### bench-path (1000j × 4 cellules mod-4)

1. `make bench-path` en background — sortie dans le task notification handler.
2. À la complétion, parser les lignes :
   ```
    offset       PnL    PnL%        α   vs BTC   win%  trades     DD%    time
   ```
3. Présenter :
   - **Tableau avant/après** des 4 cellules (PnL + DD + trades)
   - **Médiane** post-change vs baseline `BACKTEST_LOG.md` (actuellement
     **$64.66 médian, −45% DD médian** post-fix FNG, mesuré 2026-06-09)
   - **Spread + σ** : si la variance inter-cellule a augmenté, c'est suspect
     (la modif crée du chaos là où il n'y en avait pas)
   - Verdict bref : SHIP (médiane ↑ ou DD ↓ sans dégrader l'autre), MIXED
     (un meilleur, l'autre pire), HOLD (régression nette)

### bench / bench-fast / bench-full (système d'apprentissage)

Suivre le flow existant :
1. Préflight : `.env` a `GEMINI_API_KEY` ; `eval/scenarios/holdout/compact/`
   contient 3 fichiers ; playbook + behavior à jour en DB.
2. Lancer en background.
3. À la complétion : lire `eval/reports/bench/bench_*.json`, présenter
   verdict global (SHIP/MIXED/HOLD), delta `full_learning - baseline` sur
   return/alpha/DD/Sharpe, mini-bilan par scénario, et — si MIXED ou HOLD —
   identifier la couche responsable (playbook / behavior / calibration /
   thresholds) via les deltas entre variants successives.

## Comportements importants

- **bench-full = consomme ~750 RPD** : ne le suggérer qu'après une demande
  explicite ou un bench compact convaincant. Si déjà lancé une fois dans la
  journée, prévenir l'utilisateur qu'un second risque d'épuiser le quota.
- **bench-path consomme zéro RPM/RPD** — à préférer dès que le change est
  déterministe.
- **Si tous les variants montrent delta nul sur `bench`** : vérifier que
  `--provider gemini` est bien utilisé (avec `--provider rules`, les
  variants sont identiques — normal mais inutile).
- **Si bench échoue sur 429 malgré throttle 12 RPM** : le LLM cache
  préserve les appels réussis, re-lancer reprend là où ça s'est arrêté.
  Si ça persiste, la marge RPM est trop fine ce jour-là — attendre 1h ou
  baisser à 10 RPM.
- **Le rapport JSON `bench_*.json` est conservé** dans `eval/reports/bench/`
  — l'utilisateur peut y revenir après fermeture du terminal.
- **bench-path n'a pas de rapport persisté** (juste stdout) — si l'utilisateur
  veut tracer la mesure, lui suggérer d'ajouter une ligne dans `BACKTEST_LOG.md`.
