---
name: clean
description: Audit the repo for cleanliness issues (historical suffixes like _v2/_new/_legacy, dead-code fallbacks, unused functions, parallel implementations) and propose fixes. Use when the user wants a cleanup pass, after a refactor, or before opening a PR.
---

# /clean — Audit de propreté du code

Pass d'audit ponctuel pour détecter et corriger les patterns interdits par la
section « Propreté du code » du `CLAUDE.md`. À invoquer à la demande, pas
automatiquement (un cleanup pendant un dev en cours pollue le diff).

## Quand l'invoquer

- L'utilisateur dit "fais un cleanup", "/clean", "audit le code", "vérifie la
  propreté", "y'a pas du code mort ?", "on a fini un refacto, on nettoie ?"
- Après une grosse PR de refacto pour vérifier qu'on n'a pas laissé d'artefacts.
- Avant de pousser sur main si l'utilisateur le demande.

## Ne PAS invoquer pour

- Un dev en cours (le cleanup masquerait le vrai diff).
- Une demande de review fonctionnelle (utilise `/review`).
- Du lint pur (utilise `ruff` directement).

## Périmètre par défaut

Sauf instruction contraire, scanne **uniquement les fichiers modifiés depuis
`main`** (`git diff --name-only main...HEAD` + fichiers stagés/unstagés).
L'utilisateur peut demander un audit global ("scanne tout le repo") — dans ce
cas, élargis à `hellocrypto/`, `db/`, `static/js/`, `templates/`.

## Étapes

### 1. Suffixes historiques

Grepper les patterns interdits dans les **noms de fonctions, classes, variables,
fichiers** :

```bash
# Fonctions / méthodes / variables
grep -rnE '\b(def |function |const |let |var )[a-zA-Z_]+(_v[0-9]+|_new|_old|_legacy|_deprecated|_temp|_tmp|_final|_fixed)\b' \
  hellocrypto/ db/ static/js/ templates/ 2>/dev/null

# Fichiers
find hellocrypto/ db/ static/ templates/ -type f \
  \( -name '*_v[0-9]*' -o -name '*_new.*' -o -name '*_old.*' \
     -o -name '*_legacy.*' -o -name '*_deprecated.*' \)
```

**Pour chaque hit** : vérifier s'il s'agit du suffixe interdit ou d'un faux
positif (`_new_user`, `_legacy_format` qui est un vrai nom de format, etc.).
Pour un vrai hit, proposer : "renommer X_v2 → X et supprimer X" (ou "fusionner").

### 2. Fallbacks vers du code mort

Chercher les `try/except` qui chaînent une nouvelle impl vers une ancienne :

```bash
grep -rnB1 -A3 'except.*:' hellocrypto/ db/ \
  | grep -B2 -A2 -E '(repli|fallback|legacy|v1|_old)' \
  | head -40
```

**Pour chaque hit** : lire les 10 lignes autour pour comprendre. Si c'est un
vrai fallback vers une fonction obsolète, proposer la suppression. Si c'est
une vraie branche conditionnelle (ex: `if _USE_POSTGRES`), c'est OK.

### 3. Fonctions inutilisées

Pour chaque fonction publique définie dans `db/store.py` et `hellocrypto/`,
vérifier qu'elle a au moins un caller :

```bash
# Lister les définitions
grep -nE '^def [a-z_]+' db/store.py hellocrypto/**/*.py | head -50

# Pour chacune, chercher les usages (exclure la définition elle-même)
# Exemple manuel :
grep -rn "list_simulation_sessions\b" hellocrypto/ db/ static/ templates/ \
  | grep -v "def list_simulation_sessions"
```

Si zéro caller → candidat à la suppression. Attention : peut être appelé via
une route Flask (chercher le nom de route, pas juste la fonction).

### 4. Implémentations parallèles

Chercher les paires de fonctions au nom similaire qui font la même chose :

```bash
# Suffixes _v2 / _new + nom de base
grep -nE '^def [a-z_]+(_v[0-9]+|_new|_legacy)' db/store.py hellocrypto/**/*.py
# Puis vérifier si la version sans suffixe existe aussi
```

Si les deux coexistent **et** la nouvelle est utilisée partout : proposer le
plan en 3 étapes :
1. Supprimer l'ancienne
2. Renommer la nouvelle pour retirer le suffixe
3. Mettre à jour tous les callers (`grep -rn "ancien_nom_v2"`)

### 5. Commentaires interdits

```bash
grep -rnE '#.*(TODO|FIXME|XXX|HACK).*remove' hellocrypto/ db/ static/js/ \
  | head -20

grep -rnE '#.*(was:|removed:|old code|ancien code|old impl)' \
  hellocrypto/ db/ static/js/ | head -20
```

### 6. Rapport

Présenter à l'utilisateur un rapport structuré :

```
## Audit de propreté — N issues trouvées

### 1. Suffixes historiques (X)
- [db/store.py:731](db/store.py#L731) `list_simulation_sessions_v2`
  → renommer en `list_simulation_sessions`, supprimer la v1 (ligne 328),
    mettre à jour `hellocrypto/routes/simulation.py:624`

### 2. Fallbacks vers code mort (Y)
- ...

### 3. Fonctions inutilisées (Z)
- ...

### Suggestion d'ordre
1. Issue X (le plus simple, isolé)
2. Issue Y
...
```

**Ne corrige rien sans confirmation explicite.** Le but du skill est de
*surfacer* les issues. L'utilisateur choisit ce qu'on traite et dans quel ordre.

Si l'utilisateur dit "go, corrige tout" : appliquer les correctifs un par un,
en commitant chacun séparément (un cleanup atomique par commit, plus facile à
relire et à reverter).

## Limites connues

- Le skill ne détecte pas les **duplications sémantiques** (même logique avec
  des noms différents). Pour ça, lecture humaine ou outil de détection de
  clones (jscpd, sonar) — hors scope ici.
- Faux positifs possibles sur les suffixes (`api_v3` d'une lib externe,
  `legacy_endpoint` qui est le vrai nom métier). Toujours vérifier avant
  de proposer une rename.
