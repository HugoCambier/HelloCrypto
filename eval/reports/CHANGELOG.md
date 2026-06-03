# Bench Champion Changelog

Track des itérations sur le système de décision. La règle :

- Quand un nouveau bench est **meilleur** (jugement utilisateur sur metrics avg
  return / alpha / robustesse cross-scenario), il devient le nouveau champion.
- `eval/reports/champion.json` est la baseline en cours.
- Chaque entrée ci-dessous note : ce qui a changé, où ça gagne, où ça perd
  vs l'ancien champion. Conserver ces notes permet de revenir piocher les
  bonnes idées plus tard.

Outil de comparaison : `poetry run python -m hellocrypto.eval.bench_diff
eval/reports/champion.json eval/reports/bench/<latest>.json`

---

## 2026-06-03 — Stance système DEPLOY/SELECTIVE/PRESERVE (bench_20260602_194122)

**Ce qui a changé** : ajout d'un système de régime stance dans
[hellocrypto/deciders.py](hellocrypto/deciders.py) (`_derive_stance` + `STANCE_PARAMS`).
Selon BTC trend + breadth marché, le décideur entre en mode :
- **DEPLOY** (BTC haussier + bull ≥ bear) → seuil 6, top_n 4
- **SELECTIVE** (défaut) → seuil 7, top_n 3
- **PRESERVE** (BTC baissier) → seuil 8, top_n 2

Les params UI restent prioritaires (`user_pinned` guard). Backtest : checkbox stance
pour activer/désactiver. Eval runner : ne pin plus `buy_threshold` si valeur = défaut
(libère la modulation par stance).

**Diff vs champion précédent (bench_20260602_171056)** :

| variant    | scenario      | champ ret/α        | new ret/α          | Δret    | Δα      |
|------------|---------------|--------------------|--------------------|---------|---------|
| rules_only | greed_bull    | +1.67% / +2.25%    | +1.88% / +2.46%    | +0.20pp | +0.20pp |
| rules_only | neutral_bull  | -0.33% / -0.35%    | -0.38% / -0.41%    | -0.05pp | -0.05pp |
| rules_only | fear_bear     | +0.00% / +0.71%    | +0.00% / +0.71%    | 0       | 0       |
| others     | all           | —                  | —                  | 0       | 0       |

**Wins** :
- `rules_only` gagne +0.20pp alpha sur `greed_bull` → DEPLOY stance (seuil 6, top_n 4)
  capte plus d'opportunités en bull market, exactement le comportement voulu.
- `fear_bear` inchangé → PRESERVE seuil 8 filtre correctement, pas de suractivité en bear.
- Moyenne `rules_only` : **+0.05pp** ret/alpha.

**Losses** :
- `neutral_bull` perd -0.05pp : SELECTIVE (seuil 7) génère légèrement plus de bruit que
  l'ancien seuil fixe 8 sur ce scénario. Marginal et acceptable.

**Pistes suivantes** :
- `bench-ollama-full` (7j) pour valider sur horizon plus long
- Tuner `min_hold_hours` / `trend_confirm_hours` : 8h/12h comme compromis court⇔long
- Ajouter timing params dans `STANCE_PARAMS` (DEPLOY → confirm plus court pour réagir plus vite)

---

## 2026-06-02 — Bench `rules_only` aligné sur la prod (bench_20260602_171056)

**Ce qui a changé** : `_rule_based_decision` (simple buy-top-score / sell-low-score,
3e décideur historique) supprimé de [eval/runner.py:185](hellocrypto/eval/runner.py).
Le variant `rules_only` du bench appelle maintenant directement `regime_decision`
de [hellocrypto/deciders.py](hellocrypto/deciders.py) — le décideur déterministe
**en prod**. Bench, sim, réel et backtest partagent maintenant exactement le même
code de décision. Plus de 3e décideur fantôme.

**Diff vs champion précédent** :

| variant         | champ ret/α    | new ret/α      | Δret    | Δα      | verdict |
|-----------------|----------------|----------------|---------|---------|---------|
| rules_only      | +0.63% / +1.06%| +0.45% / +0.87%| -0.19pp | -0.19pp | 🔴      |
| baseline (LLM)  | +0.57% / +0.99%| +0.57% / +0.99%| 0       | 0       | ⚪      |
| playbook        | +0.48% / +0.91%| +0.51% / +0.93%| +0.03pp | +0.03pp | ⚪      |
| full_prompt     | +0.43% / +0.85%| +0.45% / +0.88%| +0.03pp | +0.03pp | ⚪      |
| calibrated      | +0.43% / +0.85%| +0.45% / +0.88%| +0.03pp | +0.03pp | ⚪      |
| full_learning   | +0.43% / +0.85%| +0.45% / +0.88%| +0.03pp | +0.03pp | ⚪      |
| regime_adaptive | -0.00% / +0.42%| -0.00% / +0.42%| 0       | 0       | ⚪      |

**Wins** :
- Alignement code : un seul décideur déterministe dans tout le repo (CLAUDE.md
  rule satisfaite). Plus de divergence entre ce qu'on benche et ce qui tourne.
- Sur **backtest 1 an** : +54pp d'alpha vs l'ancien décideur C qui tournait avant
  cette série de refactor (mesuré séparément, hors bench). Bat BTC sur 5/6 périodes.

**Losses** :
- `rules_only` perd -0.19pp d'alpha en moyenne sur les 3 scénarios held-out
  (-0.49pp sur `greed_bull`, -0.07pp sur `neutral_bull`, 0 sur `fear_bear`).
- **Cause** : les scénarios held-out durent 24 jours (1d × 24 cycles). Notre
  décideur a des frictions volontaires (`min_hold_hours=12`, `trend_confirm_hours=24`)
  conçues pour filtrer le bruit sur **longue durée**. Sur 24 jours, ces frictions
  coûtent quelques points base au reactif simple baseline. C'est le trade-off
  voulu (long terme > court terme).

**Pistes d'amélioration suivantes** :
- Lancer `bench-ollama-full` (held-out 7j × longer cycle) pour mesurer sur un
  horizon plus représentatif de la prod
- Tuner `min_hold_hours` / `trend_confirm_hours` : peut-être 8h / 12h donnerait
  un meilleur compromis court ⇔ long
- Le LLM `regime_adaptive` est le moins bon — investiguer pourquoi sa
  modulation par régime dégrade au lieu d'aider

---

## 2026-06-02 — Initial champion (bench_20260602_144851)

**Contexte** : premier snapshot capturé avant d'aligner le décideur `rules_only`
sur la vraie `regime_decision` live. Le variant `rules_only` ici est l'ancien
`_rule_based_decision` (3e décideur, simple, score-only).

**Metrics (avg across 3 holdout scenarios)** :

| variant         | ret%   | α%     | trades |
|-----------------|--------|--------|--------|
| rules_only      | +0.63  | +1.06  | 4.3    |
| baseline (LLM)  | +0.57  | +0.99  | 16.7   |
| playbook        | +0.48  | +0.91  | 20.3   |
| full_prompt     | +0.43  | +0.85  | 22.7   |
| calibrated      | +0.43  | +0.85  | 22.7   |
| full_learning   | +0.43  | +0.85  | 22.7   |
| regime_adaptive | -0.00  | +0.42  | 18.7   |

**Lecture** : le simple `_rule_based_decision` bat les variants LLM. Les
variants learning n'apportent pas d'edge mesurable sur cette held-out.
