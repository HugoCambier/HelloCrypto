# Backtest log

Track des runs 1000j (budget $100, watchlist 10 coins, risk_level 7,
stop-loss 21%, trailing 10%) pour garder une trace des directions explorées
et fermées.

**Variance start-time — réalité mod-4** (mesuré 2026-06-09 sur 10 coins) :

Sur `decide_every_n_candles=4` (décideur toutes les 4h), le start_ms
détermine le **calendrier des décisions**. Deux runs avec des starts
distants d'un multiple de 4h voient les mêmes klines aux mêmes positions
relatives → PnL strictement identique. Deux runs avec un décalage de 1h,
2h ou 3h voient des klines différentes → trajectoires différentes.

Il y a donc **4 trajectoires distinctes** selon `(start_ms // 1h) mod 4`.
Bench `--start 2023-09-13 --offsets 0,1,2,3 --days 1000` :

| offset (mod 4) | PnL | Trades | DD |
|---|---|---|---|
| +0h (mod 0) | $44.52 | 377 | -40% |
| **+1h (mod 1)** | **$110.03** | 444 | -38% |
| +2h (mod 2) | $81.37 | 370 | -31% |
| +3h (mod 3) | $67.90 | 376 | -38% |

Médiane $74.64, spread $65.51, σ $27.35. **Tout run isolé du dashboard
est donc une trajectoire parmi 4** ; comparer deux versions du code via
un seul run par version est du bruit pur.

**Protocole de mesure** :

| Type d'itération | Outil | Mesure quoi |
|---|---|---|
| Rapide (tweak local) | `make bench-path` (~15 min) | Path-dependence sur 1000j réels, médiane des 4 cellules mod-4 |
| Structurante (logique de décision) | `make bench` (~15 min, LLM) | A/B système d'apprentissage sur scenarios held-out |
| Smoke-test logique | `make bench-fast` (~1 min) | Rules-only sur scenarios courts |

- Toute comparaison single-run dashboard = **bruit** (cellule choisie au hasard par l'heure de lancement).
- Comparer baseline vs variante = **médiane des 4 cellules** (pas le best).
- Le précédent claim ($73.79 strictement identique sur 7 runs) était
  vrai mais trompeur : tous les offsets testés (0/8/16/24/48/72h) étaient
  ≡ 0 mod 4 → même cellule unique.

**Source de variance résiduelle (single-cell)** : `_fetch_klines` plantait
silencieusement sur les timeouts Binance → coins exclus sans warning.
Fixé dans `006f303` (retry + fail-loud). Plus besoin du caveat ±$15
*au sein d'une même cellule mod-4*.

## Runs mesurés

| Date | Commit | Config | PnL | DD | Verdict |
|---|---|---|---|---|---|
| 2026-06-05 *(noté)* | ?? | non identifié | **+$95** | ? | 🎯 cible — peut être variance haute |
| 2026-06-08 | `b652fe1` | sans aucun des 3 changements Friday | +$47 | -31% | baseline basse |
| 2026-06-08 | `0eb50b4` | + scoring momentum 24h, PRESERVE top_n=0 | +$68 | -32% | scoring + top_n=0 → +$21 |
| 2026-06-08 | `261d2ca` | + top-up DEPLOY/SELECTIVE | +$79.4 | -39% | top-up → +$11 |
| 2026-06-08 | `48b5b2a` (HEAD) | + early-exit | +$73.6 | -37% | early-exit → -$6 (régression sur PnL, DD ~stable) |
| 2026-06-08 | `b0274f2` | + **garde-fou top-up** (no DCA on losing) | **+$81.4** | **-30.1%** | 🏆 **best PnL ET DD** — le revert `fbd0e50` était une erreur |
| 2026-06-08 | `583f597` (revert via `cc0d2bb`) | + early-exit zombie 100h/-3% | +$164.7 | **-37.1%** | ❌ DD régresse, early-exit -$148.5 sur 48 trades — porte fermée |
| 2026-06-09 | HEAD + FNG live (bug) | bench-path médiane sur 10 coins | +$74.6 | -37% | ⚠️ artificiellement gonflé par FNG=10 live appliqué aux 1000j |
| 2026-06-09 | HEAD + FNG historique (fix) | bench-path médiane (baseline transitoire) | +$64.7 | -45% | baseline honnête mais sous-exposé aux montées BTC |
| 2026-06-10 | + BTC asym sizing | DEPLOY ×2 / SELECTIVE ×1.5 sur BTC, cap 65% cash | +$89.0 | -38.9% | médiane +$24, DD allégé de 6.5pts |
| 2026-06-10 | + top_n adaptatif strong-DEPLOY | breadth bull ≥70% → top_n=4→2 (concentration BTC + 1 alt) | **+$107.1** | **-36.4%** | 🎯 **nouveau baseline — α +$5 vs BTC, 82% de la perf BTC captée** |

## Baseline honnête post-fix FNG (2026-06-09)

Avant le fix, `fear_greed` était fetché LIVE au démarrage du backtest et
appliqué uniformément aux 1000 jours simulés. Avec FNG=10 (extreme fear)
au moment des runs récents, ça plaquait un `-1 buy_threshold` artificiel
sur toute la fenêtre → entries plus permissives, PnL gonflé, faux signaux
d'optimisation. Fix : `get_fear_and_greed_history()` indexé par date,
chaque cycle voit la valeur réelle du jour simulé.

Effet du fix sur les 4 cellules mod-4 (start=2023-09-13, 1000j) :

| offset | PnL avant | PnL après | Δ |
|---|---|---|---|
| +0h | $44.52 | $59.98 | +$15 |
| +1h | $110.03 | $79.38 | **−$31** |
| +2h | $81.37 | $54.97 | −$26 |
| +3h | $67.90 | $69.34 | +$1 |
| **médiane** | **$74.64** | **$64.66** | **−$10** |
| spread | $65.51 | **$24.41** | **−63%** |
| DD médiane | −37% | **−45%** | aggravé |

Variance inter-cellule chute de 63% (cohérence retrouvée). Le DD plus
profond révèle le vrai risque masqué par le bug. **Le $110 d'avant
n'existait pas — c'était un artefact mesure.**

## BTC asymmetric sizing (2026-06-10)

Le diag CSV montrait qu'on capturait ~50% de la perf BTC (médiane $65 vs
BTC ~$130) parce qu'on dilue capital sur 10 coins quand BTC seul rideait
la tendance. Ajout d'un facteur de conviction BTC dans `_btc_conviction_mult`:
- DEPLOY → BTC ×2.0
- SELECTIVE → BTC ×1.5
- PRESERVE / CASH → ×1.0 (defensive ne concentre pas)

Hard cap à 65% du cash_after par position pour éviter que BTC rafle tout
quand il sort en rank 1 du tri par score.

Bench-path post-change (start=2023-09-13, 1000j, 10 coins):

| offset | avant | après | Δ |
|---|---|---|---|
| +0h | $59.98 | $88.23 | +$28 |
| +1h | $79.38 | $89.71 | +$10 |
| +2h | $54.97 | $38.17 | **−$17** |
| +3h | $69.34 | $99.09 | +$30 |
| **médiane** | **$64.66** | **$88.97** | **+$24 (+38%)** |
| DD médiane | −45.4% | **−38.9%** | +6.5 pts |
| trades médiane | 417 | 352 | −65 |
| α vs BTC médian | −$44 | **−$13** | +$31 |

3 cellules sur 4 améliorent. Cellule +2h régresse de $17 — un mauvais
timing d'entrée BTC sur cette trajectoire, mais médiane > best of baseline.
Variance inter-cellule monte ($24 → $61 spread) — attendu vu la
concentration. On capture maintenant ~68% de la perf BTC (vs 50% avant).

## Top_n adaptatif en strong-DEPLOY (2026-06-10)

Sur l'angle BTC asym seul, on plafonnait à ~68% de la perf BTC. Hypothèse :
en DEPLOY normal `top_n=4`, mais après le BTC boost (65% cash) + position 2
(~16%), les positions 3 et 4 récupèrent des miettes (5%/3%) — assez gros
pour prendre des micro-losses, trop petits pour matter en gains. Donc on
porte du risque sans upside.

Fix : quand DEPLOY fire ET breadth bull ≥ 70% (≥7 coins sur 10 en trend_1d
haussier), `top_n` passe de 4 à 2. SELECTIVE / PRESERVE / CASH inchangés.

Bench-path post-change (start=2023-09-13, 1000j, 10 coins) — compose
avec le BTC asym sizing du commit précédent :

| offset | BTC asym seul | + top_n adaptatif | Δ |
|---|---|---|---|
| +0h | $88.23 | $145.40 | **+$57** |
| +1h | $89.71 | $99.86 | +$10 |
| +2h | $38.17 | $84.37 | **+$46** |
| +3h | $99.09 | $114.24 | +$15 |
| **médiane** | $88.97 | **$107.05** | **+$18 (+20%)** |
| DD médiane | −38.9% | **−36.4%** | +2.5 pts |
| α vs BTC médian | −$13 | **+$5** | +$18 |
| trades médiane | 352 | 325 | −27 |

**Les 4 cellules améliorent.** Cellule worst-case +2h passe de $38 à $84 —
la trajectoire fragile devient nette. **α positif pour la première fois.**
On capture maintenant **82% de la perf BTC** (vs 68% commit précédent,
50% baseline FNG-fix).

Mécanique probable : moins de positions tail = moins de bleeds sur des
alts faibles, capital qui reste cash plutôt qu'allocué à des micro-positions
qui pourrissent. Le combo {BTC ×2 conviction} + {top_n=2 en strong-DEPLOY}
amplifie le signal en compressant le bruit.

## Découverte clé : le garde-fou top-up

Le commit `b0274f2` ("pas de DCA sur position en perte") a été reverted dans
`fbd0e50` sur la base d'un run probablement bruité. Mesure rigoureuse :

| | HEAD ($73) | b0274f2 ($81) |
|---|---|---|
| Stop-loss durs | 4 / -$48 | **1 / -$2.67** |
| Worst trade | -$26 | -$12.85 |
| Signal exits | -$15 | **+$28** |
| Profit factor | 1.36 | **1.43** |
| DD | -37% | -30.1% |

Mécanique : ne pas top-up une position rouge évite qu'un bleed devienne un
gros stop-loss dur (×4 réduit à ×1), ce qui casse la spirale qui transformait
les signal-exits en losers nets.

**Action recommandée : revert du revert.** Cherry-pick `b0274f2` sur main
(ou équivalent : `git revert fbd0e50`).

## Directions fermées (toutes régressent vs HEAD)

Tentatives session 2026-06-08, non commitées :

| Idée | Mécanique | PnL | Raison du revert |
|---|---|---|---|
| Scoring relatif (penalty-only) | cohort ranking soustrait du score absolu | $162 | scoring reste inversé (gagnants < perdants) |
| Profit-lock cliquet | peak ≥+5% → floor breakeven, peak ≥+15% → +5% | $131 | cascade re-entry : exits forcés → fresh setups dégradés |
| Trailing adaptatif | trail tighten à 7%/5%/4% selon peak | $143 | même cascade re-entry, plus discrète |
| Early-exit zombie (commité+revert) | hold ≥100h → seuil perte -5% → -3% | $164.7 | DD -37% (vs -30% baseline), early-exit -$148.5 sur 48 trades. CSV : 28 "shallow zombies" (100-200h) font -$70 de coupes marginales. Pire : zombie cut libère du cash qui se re-déploie en SL/circuit-breaker (ex BTC fév 2025 : zombie -$10.4 → top-ups → -$5 circuit-breaker). Pattern cascade re-entry confirmé pour la 4e fois. |
| CASH OR-gate (locale, revert) | `_derive_stance` : drawdown ≥7% **OR** breadth ≥70% au lieu de **AND** | $9.20 | -$65 PnL, +4 pts DD, **+447 trades** (425→872, +105%). Le AND-gate du code original avait raison : OR sur-déclenche CASH pendant les pullbacks normaux → exits forcés → re-deploy à pire prix. **5e confirmation du pattern cascade re-entry**. |

## Leçons consolidées

1. **Variance start-time** : ±$10-30 sur 1000j juste en changeant l'heure de démarrage.
2. **Cascade re-entry** : toute mécanique qui ajoute des exits libère du capital qui se redéploie souvent au mauvais moment → -$ net même si la mécanique elle-même fait gagner. **Confirmé 4× : profit-lock, trailing adaptatif, scoring relatif, early-exit zombie.** Règle empirique : ne pas chercher à améliorer les exits — chercher à filtrer les entrées (filtre macro, OR-gate CASH, etc.).
3. **Top-up DEPLOY/SELECTIVE** : +$11 mesuré.
4. **PRESERVE top_n=0** : +$21 mesuré (vs top_n=2).
5. **Early-exit** : -$6 marginal mais coupe quelques gros bleeds. À garder ou pas selon priorité PnL vs DD.
6. **Scoring momentum 24h + pullback** : utile (effet positif sur l'absolu).

## Pistes non encore testées

- **Cooldown re-deploy après exit** : attendre 24-48h après un SELL avant tout BUY (anti-cascade).
- **Filtre macro entrée** : "ne pas entrer si BTC < X% sous SMA25" (basé sur le diag signal-perdants moy +0.3% vs trailing-gagnants -0.2%).
- **Exit asymétrique green vs red** : trailing strict en perte, lâche en profit.
- **Cap hold-time** : au-delà de 90j sans nouveau plus haut, tightening progressif.
