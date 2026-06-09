# Backtest log

Track des runs 1000j (budget $100, watchlist 10 coins, risk_level 7,
stop-loss 21%, trailing 10%) pour garder une trace des directions explorées
et fermées.

**Variance start-time** : ~~un même commit lancé à des heures de démarrage
différentes (08h/10h/12h…) donne $10-30 d'écart de PnL.~~ **MESURÉ FAUX**
(2026-06-08, voir bench `scripts/bench_start_variance.py`) : 7 runs au
même commit avec offsets 0/8/16/24/48/72h → PnL strictement identique
($73.79, σ=$0).

**Vraie source de la variance** : `_fetch_klines` plantait silencieusement
sur les timeouts Binance → coins exclus sans warning → runs comparés sur
des univers différents. Fixé dans `006f303` (retry + fail-loud).

Conséquence : les comparaisons single-run SONT fiables si pas d'erreur
fetch. Plus besoin du caveat ±$15.

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
