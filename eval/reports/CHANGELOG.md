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

## 2026-06-03 — Revert ATR-adaptive trailing (critère DD déclenché)

**Ce qui a changé** :
- `check_stops` revient au trailing fixe (param `trail_stop` direct).
- `_check_stops` du backtest pareil — plus de calcul ATR par symbole.
- `ATR_TRAIL_K/MIN/MAX` supprimés, helper `_adaptive_trail_pct` supprimé.
- Le paramètre `market_raw` reste optionnel dans `check_stops` (réservé
  pour usage futur — éventuellement piste sizing par volatilité plus tard).
- `atr` reste calculé dans le snapshot marché (live + backtest enrich) —
  on garde le champ disponible.

**Motivation du revert** :
Backtest 600j post-piste-2 :
- DD : -33.9% → **-37.9%** (+4pp, critère explicite de revert du CHANGELOG)
- Trades totaux : 268 → **388** (+45%) — over-trading
- WR : 16.5% → 12.4% (-4pp)
- Total return : +34.93% → +31.13% (-3.80pp)
- vs BTC : +$27.84 → +$24.06 (-$3.78)

Le trailing ATR-adaptive faisait bien plus de sorties (+$70 trailing PnL)
mais déclenchait un cycle vicieux : exit serré → re-entrée rapide → exit
serré → … Chaque cycle accumule des micro-pertes et la friction explose
les signal exits (-$77 → -$138).

**Leçon** : un trailing plus tight améliore mécaniquement la sortie
individuelle mais dégrade le système global via le coût d'opportunité
des re-entrées et la friction (fees). Le fixed 10% restait calibré.

**Pistes encore en vie après revert** :
- F&G modulator : gardé (bench +0.80pp sur bull_to_correction, signal
  indépendant prouvé utile)
- Volume signal asymétrique : gardé (effet modeste mais sans régression
  prouvée en bench)
- Multi-timeframe trend 4h, portfolio drawdown stop, LINK→tier 8 : à
  attaquer ensuite

---

## 2026-06-03 — Fear & Greed contrarian modulator

**Ce qui a changé** :
- `regime_decision` accepte un `fng_value: int | None` optionnel.
- Si FNG ≥ 75 (extreme greed) : `buy_threshold += 1` — plus exigeant
  quand la foule est euphorique (crowd top probable).
- Si FNG ≤ 25 (extreme fear) : `buy_threshold -= 1` — plus opportuniste
  quand la foule panique (crowd bottom probable).
- Modulation skippée si `buy_threshold` est user-pinned (UI override).
- Callers updated : `eval/runner.py`, `backtest.py`, `simulation.py`
  passent maintenant la valeur F&G du moment.

**Motivation** :
- Signal indépendant des indicateurs techniques.
- F&G fetched depuis CMC, déjà persisté dans les snapshots.
- Logique contrarian classique : ça mean-reverte sur les extrêmes.

**Diff vs champion (ATR-adaptive)** :

Compact 1d : zéro changement — F&G constant sur 24h, modulation fixe.

Full 7d :
| scénario | FNG (min/avg/max) | baseline α | new α | Δ |
|----------|-------------------|----------:|------:|---|
| bull_to_correction_7d | 24/55/71 | -0.17% | **+0.63%** | **+0.80pp** |
| greed_bull_7d | 70/71/74 (steady greed) | +0.93% | +0.97% | +0.04pp |
| neutral_bull_7d | 38/43/50 (neutral, no mod) | -1.07% | -1.03% | +0.04pp |
| fear_bear_7d | 8/9/12 (no positions) | -1.27% | -1.27% | 0 |

**Wins** :
- **+0.80pp alpha sur bull_to_correction** : FNG démarre à ~70 en début
  de scenario (greed) → threshold +1 → moins d'entrées agressives juste
  avant la correction → moins de DD.
- Neutre ailleurs (no regression).

**Pourquoi ça marche** : F&G capture une dimension du marché (sentiment
crowd) que les indicateurs techniques ne voient pas. Quand FNG est haut,
la majorité est long → asymmétrie risk/reward défavorable pour entrer.

**Pistes suivantes** :
- Recalibrer ATR-adaptive si user 600j confirme régression
- Multi-timeframe trend (4h en plus de 1h et 1d)
- LINK → tier 8

---

## 2026-06-03 — ATR-adaptive trailing stop

**Ce qui a changé** :
- `trading.check_stops` accepte un `market_raw` optionnel et dérive le
  trailing % par symbole : `trail = clamp(5%, 15%, K=5 × ATR/price)`.
- Backtest (`_check_stops`) calcule ATR sur les 14 dernières bougies du
  symbole et applique la même formule.
- Eval runner et strategy.apply_paper_stops thread `market_raw`.
- Fallback fixe (10%) si ATR indisponible.

**Motivation** :
Trailing fixe à 10% est trop tight sur BTC (atr_pct ~0.5% → grosse marge),
trop loose sur DOGE/POL volatils (atr_pct ~2-3%). ATR-adaptive donne à
chaque position une marge calibrée sur sa volatilité propre.

**Diff vs champion (volume signal)** :

Compact 1d : zéro changement — le trailing ne fire pas en 24 cycles.

Full 7d (test interne) :
| scénario | baseline α | ATR α | Δ |
|----------|----------:|------:|---|
| bull_to_correction_7d | -1.75% | **-0.17%** | **+1.58pp** |
| greed_bull_7d | +3.43% | +0.93% | **-2.50pp** |
| neutral_bull_7d | -1.08% | -1.07% | ~0 |
| **Moyenne** | **+0.20%** | **-0.10%** | **-0.30pp** |

**Trade-off fondamental** :
- Trail plus tight → exit plus tôt sur les vraies corrections (gain
  bull_to_correction +1.58pp d'alpha, DD -11.3% → -9.85%)
- Trail plus tight → exit aussi sur pullbacks transients qui se reprennent
  (perte greed_bull -2.50pp, on coupe avant le rebond)

**Décision de ship** :
Bench montre 7j moyenne -0.30pp avg. **Ce n'est PAS un clear win.** On
ship en pariant sur le 600j user : si la distribution des événements en
favorise les vraies corrections (vs pullbacks transients), ATR-adaptive
gagne. Sinon on revert.

**Critère de revert** :
- Si DD 600j user augmente OU
- Si trailing-stops PnL passe en dessous de +$70 (vs +$82 actuel)

→ revert vers fixed 10%.

**Pistes suivantes** :
- Piste 3 : F&G index comme stance-modulator (signal indépendant)
- LINK → tier 8 si confirmé loser persistant en 600j

---

## 2026-06-03 — Volume confirmation (asymétrique, additif)

**Ce qui a changé** :
- Nouveau champ `volume_ratio_1h` dans `get_enriched_market_data` :
  ratio du volume de la dernière bougie 1h sur la moyenne 24h.
- Nouveau champ aussi dans le scenario builder (`build_holdout_scenarios.py`)
  pour que le bench puisse mesurer le signal.
- Ajout dans `compute_score_rules` : si `volume_ratio_1h > 1.5` ET
  `change_pct_1h > 0` → **+1** (real buying flow). Asymétrique : pas de
  pénalité sur high-vol red (peut être capitulation/shake-out).
- SMA7/25 cross **conservé** (premier essai en remplacement régressait
  -0.26pp : retirer un +1 structurel sur les setups bullish a déplacé
  les scores et coupé des trades borderline).

**Diff vs champion (bench_20260603_084403)** :

Compact 1d : zéro changement — `volume_ratio` reste < 1.5 ou
accompagne du rouge dans ces fenêtres. Le signal n'a pas l'occasion
de tirer.

Full 7d :
| scénario | baseline α | new α | Δ |
|----------|----------:|------:|---|
| bull_to_correction_7d | -1.96% | **-1.75%** | +0.21pp |
| greed_bull_7d | +3.43% | +3.43% | 0 |
| neutral_bull_7d | -1.08% | -1.08% | 0 |

**Wins** :
- +0.21pp alpha sur bull_to_correction_7d : le signal vol détecte
  quelques accumulations bull en debut de scénario, le bonus +1 tire
  un setup au-dessus du seuil au bon moment.
- Aucune régression.

**Trade-offs** :
- Impact modeste — le signal asymétrique fire rarement (volume_ratio>1.5
  AND green hour). C'est intentionnel : un filtre de qualité, pas un
  driver de score.

**Pistes suivantes** :
- Piste 2 : ATR-adaptive trailing stop (mesurer +$82 → +$120+ ?)
- Piste 3 : F&G index comme stance-modulator
- Investigation : pourquoi le volume signal ne fire qu'en 7j ? Possible
  qu'on doive abaisser le seuil à 1.3 ou ajouter un signal volume
  basé sur la moyenne mobile plutôt que ratio strict.

---

## 2026-06-03 — trend_confirm_hours par stance + AND-gate CASH

**Ce qui a changé** :
- `trend_confirm_hours` devient stance-dépendant dans `STANCE_PARAMS` :
  DEPLOY/SELECTIVE 36h (au lieu de 24h), PRESERVE/CASH gardent 24h.
- **Plus important** : `_derive_stance` passe en **AND-gate** pour CASH.
  Avant : drawdown ≥7% OR breadth bear ≥70% (sensible). Maintenant : les
  deux conditions doivent être vraies. Évite les faux positifs lors des
  pullbacks bull normaux qui font brièvement flipper le breadth intraday.

**Motivation** :
- En testant DEPLOY/SEL 48h, on a découvert que CASH OR-gate causait
  -3.78pp alpha sur greed_bull_7d : breadth intraday flippait à 70%
  brièvement pendant un pullback bull normal, CASH se déclenchait, on
  sortait au mauvais moment.
- L'AND-gate est plus restrictif mais bien plus précis : CASH ne fire
  qu'en correction *broad AND deep*, pas sur un blip horaire.

**Diff vs champion précédent (bench_20260603_084403)** :

Compact 1d : zéro changement (CASH fire au même cycle dans
bull_to_correction_1d, où les deux conditions étaient déjà remplies).

Full 7d (test interne) :
| scénario | baseline α | new α | Δ |
|----------|----------:|------:|---|
| bull_to_correction_7d | -1.62% | -1.96% | **-0.34pp** |
| fear_bear_7d | -1.27% | -1.27% | 0 |
| greed_bull_7d | +2.30% | **+3.43%** | **+1.14pp** |
| neutral_bull_7d | -0.82% | -1.08% | -0.26pp |
| **Moyenne** | **-0.35%** | **-0.22%** | **+0.18pp** |

**Wins** :
- greed_bull_7d +1.14pp : plus de faux CASH sur pullbacks normaux.
- Pas de régression dramatique : -0.34pp sur bull_to_correction (CASH
  déclenche un peu plus tard maintenant que les deux conditions sont
  requises, mais finit par se déclencher).
- Moyenne +0.18pp.

**Trade-offs** :
- bull_to_correction perd 0.34pp d'alpha — la protection bear est un peu
  plus tardive. C'est le prix pour éviter les faux positifs en bull.

**Pistes suivantes** :
- Piste 1 : signal cleanup (dedupe SMA + ajout trend_short)
- Trailing stop ATR-adaptatif
- Mesurer impact sur backtest 600j user (le vrai juge)

---

## 2026-06-03 — Score-based exit guard (stance-dépendant)

**Ce qui a changé** :
- Nouveau param `score_exit_threshold` dans `DEFAULTS` (5). En plus du timer
  `trend_confirm_hours` baissier, l'exit n'est déclenché que si le score
  holistique du symbole est tombé **sous** ce seuil. Anti-whipsaw.
- Calibration **stance-dépendante** dans `STANCE_PARAMS` :
  - DEPLOY / SELECTIVE → seuil **5** (gate ON : let winners run, ignorer le
    bruit intraday qui ne se reflète pas dans le score global)
  - PRESERVE / CASH → seuil **99** (gate OFF : sortir vite, ne pas
    second-guess le signal défensif)

**Motivation** :
- Backtest 600j utilisateur : sorties par signal = **-$62 net** sur 211
  trades (-$0.29 / trade). Le timer `trend baissier` exit sortait sur des
  bruits intraday alors que le setup global restait sain.
- Première version (gate universel à 5) testée sur scénarios 7j :
  -0.53pp alpha sur `bull_to_correction_7d`. **Régression confirmée** :
  en correction, `trend_1d` reste haussier (SMA 25j lente) → score reste
  6-8 → gate bloque l'exit nécessaire → on saigne.
- Version stance-dépendante : gate actif uniquement quand on **veut** tenir
  (DEPLOY/SELECTIVE). En défense (PRESERVE/CASH), on ne filtre pas — on
  sort sur le signal direct.

**Diff vs champion précédent (bench_20260603_091243)** :

Bench compact (1j) : zéro changement sur les 4 scénarios — le gate ne se
déclenche pas dans ces fenêtres trop courtes (peu d'exits par signal).

Bench full (7j) test interne : zéro régression vs gate OFF — la version
stance-dépendante est neutre sur ces scénarios car les exits se font en
PRESERVE/CASH (gate off par design) et DEPLOY n'a pas d'exits dans ces
fenêtres.

**Validation requise** :
Le bénéfice attendu (-$62 → ≥0 sur les signal exits) ne se mesure pas
dans les scénarios held-out actuels. Le change est shippé sur **base
théorique** + données du backtest 600j. **Re-run backtest 600j requis**
pour valider :
- Si `signal exits` PnL passe de -$62 vers neutre ou positif → on garde
- Si dégradation → on revert vers gate OFF universel

**Pistes suivantes** :
- Construire un scénario held-out type "ride_through_pullback" (bull
  continu avec 2-3 jours de pullback puis reprise) pour mesurer le gate
- Piste 1 : dédoublonnage SMA + ajout `trend_short` dans le scoring
- Trailing stop ATR-adaptatif

---

## 2026-06-03 — Buy_threshold +1 par stance + risk-tier coin filter

**Ce qui a changé** :
- `STANCE_PARAMS` : `buy_threshold` augmenté de +1 sur les 3 stances actifs
  (DEPLOY 6→7, SELECTIVE 7→8, PRESERVE 8→9, CASH reste 11). Le backtest 600j
  montrait que score 8 ne discriminait pas (winners 8.76 / losers 8.56) →
  on relève la barre.
- Nouveau module [hellocrypto/coin_tiers.py](hellocrypto/coin_tiers.py) :
  `COIN_RISK_TIERS` mappe chaque coin sur un tier 2-8, calibré sur la perf
  du backtest 600j. Filtre appliqué à l'entrée dans `regime_decision` :
  un coin n'est candidat à l'achat que si son tier ≤ `risk_level` user.
  Les sorties ne sont JAMAIS filtrées (positions tenues toujours liquidables).

**Tiers calibrés sur le backtest 600j** :

| Tier | Coins | Justification |
|------|-------|---------------|
| 2 | BTC | blue chip |
| 3 | ETH | blue chip |
| 4 | BNB | top exchange |
| 5 | SOL, XRP, DOGE | matures + DOGE gagnant du backtest (+$26) |
| 6 | AVAX | neutre dans le backtest |
| 7 | ADA, LINK | -$18 / -$21 sur 600j, faible signal-to-noise |
| 8 | POL | pire perdant (-$7 sur peu de trades) |

À risk_level=5 (défaut bench) : 6 coins admis (BTC ETH BNB SOL XRP DOGE).
À risk_level=7 (défaut backtest UI) : 9 coins (+ AVAX ADA LINK).
À risk_level=10 : tout autorisé.

**Diff vs champion précédent (bench_20260603_084403)** :

| variant    | scenario             | champ ret/α        | new ret/α          | Δret    | Δα      |
|------------|----------------------|--------------------|--------------------|---------|---------|
| rules_only | bull_to_correction   | -5.74% / +1.52%    | -5.42% / +1.85%    | +0.33pp | +0.33pp |
| rules_only | fear_bear            | +0.00% / +0.71%    | +0.00% / +0.71%    | 0       | 0       |
| rules_only | greed_bull           | +1.88% / +2.46%    | +1.59% / +2.17%    | -0.29pp | -0.29pp |
| rules_only | neutral_bull         | -0.38% / -0.41%    | +0.05% / +0.03%    | +0.43pp | +0.43pp |

**Wins** :
- Moyenne alpha **+0.12pp**.
- `bull_to_correction` +0.33pp (effet piste 2 — seuils plus exigeants
  filtrent les setups marginaux pendant les transitions).
- `neutral_bull` +0.43pp (effet piste 5 — exclusion de POL/ADA qui pèsent
  négativement dans ce régime).

**Losses** :
- `greed_bull` -0.29pp : ADA et AVAX ont été des trades gagnants dans ce
  scénario spécifique du 2025-07-20 ; le tier filter les exclut. C'est un
  cost attendu — sur 600j ces coins sont collectivement perdants. Tradeoff
  cohérent avec l'analyse du backtest user (drop POL/ADA/LINK = +$45).

**Pistes suivantes** :
- Piste 1 : dédoublonnage SMA + ajout `trend_short` au scoring
- Piste 4 : score-based exit guard (ne pas sortir sur signal si score
  reste fort, anti-whipsaw vs les -$60 de signal-exits)
- Trailing stop ATR-adaptatif (au lieu de hard 10%)
- Si bench-ollama-full disponible : valider sur scénarios 7j

---

## 2026-06-03 — CASH stance + exit signal stance-dépendant (bench_20260603_084403)

**Ce qui a changé** :
- Ajout du stance **CASH** dans [hellocrypto/deciders.py](hellocrypto/deciders.py),
  déclenché par des signaux **leading** (et non par `trend_1d` retardé) :
  - BTC drawdown ≥ 7% depuis le high 7j, OU
  - breadth intraday (`trend` 1h) bear ≥ 70%
- En CASH : `top_n=0` → blocage de toute nouvelle entrée.
- **Signal de sortie maintenant stance-dépendant** :
  - DEPLOY/SELECTIVE → `trend_1d` (lent, OK en bull, évite la panique)
  - PRESERVE/CASH → `trend` (1h SMA cross, ~25h) → sortie en heures au lieu
    de semaines en marché descendant.
- Trackers `bear_since_1d` et `bear_since_1h` maintenus en parallèle pour
  permettre le switch sans perdre d'historique.
- Nouveau champ `drawdown_pct_7d` calculé dans
  [hellocrypto/api.py](hellocrypto/api.py) (live) + builder de scénarios.
- Nouveau scénario holdout **bull_to_correction** (semaine de l'ATH BTC
  $126k → $102k en oct 2025) — mesure explicitement la protection bear.

**Diff vs champion précédent (bench_20260602_194122)** :

| variant    | scenario              | champ ret/α        | new ret/α          | Δret    | Δα      |
|------------|-----------------------|--------------------|--------------------|---------|---------|
| rules_only | greed_bull            | +1.88% / +2.46%    | +1.88% / +2.46%    | 0       | 0       |
| rules_only | neutral_bull          | -0.38% / -0.41%    | -0.38% / -0.41%    | 0       | 0       |
| rules_only | fear_bear             | +0.00% / +0.71%    | +0.00% / +0.71%    | 0       | 0       |
| rules_only | **bull_to_correction**| —                  | **-5.74% / +1.52%**| —       | —       |

**Wins** :
- **+1.52pp alpha** sur le nouveau scénario corrective. BTC perd ~7% en 24h ;
  on perd 5.74% en se défendant via CASH (h0/h18-23) + sorties accélérées.
- Aucune régression sur les 3 scénarios existants.
- Backtest 600j local (Hugo) montre alpha positif vs buy-and-hold (+$21 sur
  $100 budget), mais drawdown encore -43% → le CASH stance protège les
  entrées, pas suffisamment les positions tenues.

**Losses / caveats** :
- Pas de contrefactuel direct "champion sur le même scénario" (le scénario
  n'existait pas dans le champion).
- Backtest 600j révèle que les **sorties par signal sont net négatives**
  (-$60 sur 223 trades vs trailing-stop +$100 sur 21 trades). Accélérer
  les sorties via `trend` 1h sur PRESERVE/CASH pourrait amplifier ce
  pattern sur marchés mixtes — à surveiller au prochain bench long.

**Pistes suivantes** :
- Dédoublonnage scoring : `sma7>sma25` redondant avec `trend` dans
  `compute_score_rules`. Ajouter `trend_short` (30m/5m).
- Buy threshold par stance (DEPLOY 7 / SELECTIVE 8 / PRESERVE 9) :
  être plus exigeant à l'entrée quand le régime se dégrade.
- Score-based exit guard : ne pas sortir sur signal si le score reste fort
  (anti-whipsaw, contre le -$60 des signal exits).
- Risk-tier par coin pour filtrer la watchlist selon `risk_level`
  (LINK, ADA, POL coûtent -$45 cumulés sur 600j).

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
