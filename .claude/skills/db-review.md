---
name: db-review
description: Audit un diff (ou le repo) sous l'angle egress + stockage Supabase free tier — lectures sur chemins pollés, SELECT *, scans non bornés, écritures par tick, champs volumineux ajoutés à des réponses pollées. Use when the user wants to check the DB/egress cost of a change, before pushing something touching db/ or a polled endpoint, or asks "ça coûte combien en egress ?".
---

# /db-review — Audit egress & stockage

Pass d'audit ponctuel pour vérifier qu'un changement respecte la section
**« Base de données — egress & stockage (hard constraint) »** du `CLAUDE.md`.
À invoquer à la demande (avant de pousser une modif DB, ou en review).

Rappel des quotas free tier qui mordent : **egress 5 GB/mois**, **DB size 0.5 GB**.
Objectif : garder l'app fluide.

## Quand l'invoquer

- "ça coûte combien en egress ?", "/db-review", "audite la conso DB", "on dépasse
  pas le free tier là ?", avant de pousser une modif touchant `db/` ou un endpoint.
- Après avoir ajouté un champ à une réponse d'API pollée, une nouvelle lecture, une
  table, ou un insert dans une boucle.

## Ne PAS invoquer pour

- Une review fonctionnelle (utilise `/review`) ou de propreté (utilise `/clean`).
- Une modif qui ne touche ni `db/`, ni les routes, ni un endpoint pollé.

## Périmètre par défaut

Sauf instruction contraire, analyse **uniquement le diff** (`git diff main...HEAD`
+ stagé/unstagé). L'utilisateur peut demander un audit global du repo.

## Chemins pollés (à connaître)

Endpoints rafraîchis périodiquement côté front — chaque octet y est multiplié par
(onglets × fréquence × jours). Repère-les dans le diff :

- `/api/portfolio` — ~60 s
- `/api/performance` — ~60 s (fast path) ; slow path (benchmarks) caché ~5 min
- `/api/simulation/status` — ~5 s quand une sim tourne
- `/api/backtest/status` — ~1 s pendant un backtest
- `/api/market/context`, `/api/watchlist/enriched` — selon l'onglet

Fréquences exactes : grep `setInterval` dans `static/js/`.

## Étapes

### 1. Nouvelles lectures sur un chemin pollé

Repérer dans le diff toute lecture DB (`load_*`, `get_state`, `SELECT`, `.execute(`)
ajoutée dans une fonction servant un endpoint pollé.

```bash
git diff main...HEAD -- hellocrypto/routes/ hellocrypto/ db/ \
  | grep -nE '^\+' | grep -iE 'load_|get_state|select|\.execute\(|fetch' 
```

Pour chaque hit sur un chemin pollé : estimer **octets × fréquence × 30j**. Si la
valeur change rarement → proposer un **cache process** (TTL ou invalidation sur
événement), sur le modèle de `_BENCH_CACHE` (`routes/performance.py`).

### 2. SELECT * / lignes grosses

```bash
git diff main...HEAD | grep -nE '^\+' | grep -iE 'select \*|SELECT \*'
grep -rnE 'SELECT \*|select \*' db/ | head
```

Si la table est large (`trades`, `logs`, `price_snapshots`, `market_analyses`) ou
la ligne grosse (snapshots ~600 o, analyses avec `reasoning`) → proposer une
**projection de colonnes** (`columns=[...]`) limitée au strict nécessaire.

### 3. Scans non bornés

```bash
git diff main...HEAD | grep -nE '^\+' | grep -iE 'load_history|load_snapshots|FROM (trades|logs|price_snapshots)'
```

Vérifier qu'il y a un `limit`, une fenêtre temporelle (`start_ts`/`end_ts`) ou une
pagination. Un scan full-table sur `logs`/`price_snapshots` est un red flag.

### 4. Écritures par tick / en boucle

```bash
git diff main...HEAD | grep -nE '^\+' | grep -iE 'save_trade|save_log|save_snapshot|insert into|\.execute\(.*INSERT'
```

Un `INSERT` dans une boucle de cycle ou par symbole → vérifier qu'un batch/upsert
n'est pas préférable, et qu'une purge existe pour le volatile (snapshots 5min > 7j,
logs > 14j).

### 5. Champ ajouté à une réponse pollée

Repérer un nouveau champ dans un `jsonify({...})` d'un endpoint pollé, surtout un
**tableau** (timeseries, history, price_series). Estimer la taille du payload
ajouté × fréquence. Préconiser de le **gater** derrière un flag (`with_xxx=1`)
servi seulement quand c'est nécessaire, ou de le sortir du poll (cf. `with_prices`
sur `/api/performance`, servi uniquement à l'ouverture de l'onglet, pas au poll 60s).

### 6. Croissance du stockage

```bash
# Nouvelles colonnes / tables dans le diff
git diff main...HEAD -- db/store.py db/snapshots.py | grep -nE '^\+.*(CREATE TABLE|ADD COLUMN|INSERT)'
```

Une nouvelle table ou un insert récurrent : estimer lignes/jour × taille ligne, et
vérifier qu'une rétention/purge est prévue (DB cap = 0.5 GB).

### 7. Rapport

```
## Audit egress & stockage — N points

### 🔴 Bloquant
- [routes/performance.py:712](...) `price_series` (tableau ~K o) ajouté à la
  réponse de `/api/performance`, pollé 60s → ~X Mo/mois/onglet.
  → gater derrière `with_prices=1` (déjà le cas ✓) / sortir du poll.

### 🟠 À surveiller
- [...] `SELECT *` sur `trades` → projeter les colonnes lues.

### 🟢 OK
- [...] lecture mono-ligne `get_state`, négligeable.

### Estimation
- Egress ajouté : ~X Mo/mois (vs quota 5 GB)
- Stockage ajouté : ~Y Mo/mois (vs cap 0.5 GB)
```

**Ne corrige rien sans confirmation.** Le but est de *surfacer* le coût et proposer
des optimisations chiffrées. L'utilisateur décide.

## Limites connues

- Les estimations d'egress sont des ordres de grandeur (dépendent du nombre
  d'onglets ouverts et de la fréquence réelle) — viser le bon ordre de grandeur,
  pas la précision à l'octet.
- Ne mesure pas l'egress *réel* observé : pour ça, le dashboard Supabase
  (Reports → Usage). Ce skill raisonne sur le diff, en amont.
