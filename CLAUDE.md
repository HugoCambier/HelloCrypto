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

## Propreté du code

Règles à appliquer **par défaut** quand tu écris ou modifies du code. Si une règle
te semble bloquante dans un cas précis, demande avant de l'enfreindre.

### Nommage — pas de suffixes historiques

Interdits : `_v2`, `_v3`, `_new`, `_old`, `_legacy`, `_deprecated`, `_temp`, `_tmp`,
`_final`, `_real`, `_fixed`. Si tu introduis une nouvelle version d'une fonction :

- Renomme l'ancienne en gardant le nom canonique, supprime-la, ou fusionne.
- **Ne livre jamais** deux implémentations parallèles d'une même responsabilité
  avec un suffixe numérique. C'est du code mort en sursis.
- Si la transition est non-triviale (vieux callers à migrer, vieille DB à supporter)
  : signale-le explicitement à l'utilisateur, ne le décide pas en silence.

### Pas de fallback vers du code mort

Pattern interdit :
```python
try:
    return new_impl()
except Exception:
    return old_impl()  # ← legacy fallback
```

Si `new_impl` est censé être la vérité, l'autre doit disparaître. Si l'ancien est
encore nécessaire (vieille DB, vieux schéma) : c'est une vraie branche conditionnelle
basée sur **l'état observable** (version de schéma, feature flag, etc.), pas un
catch-all silencieux.

### Une responsabilité, un endroit

- Si tu te retrouves à dupliquer une logique (calcul, requête, rendu HTML) à 2+
  endroits parce que c'est plus rapide : signale-le. Trois lignes similaires valent
  mieux qu'une abstraction prématurée, mais quatre c'est une fonction.
- Si tu ajoutes un champ à une réponse d'API, vérifie que les autres endpoints
  similaires (`list_simulation_sessions_v2` vs `list_real_sessions`) sont enrichis
  de manière cohérente.

### Commentaires

Par défaut, n'en écris pas. N'écris un commentaire que si le *pourquoi* n'est pas
évident à la lecture (contrainte cachée, invariant subtil, workaround pour un bug
précis). Ne décris jamais le *quoi* — les identifiants bien nommés s'en chargent.

Interdits :
- Commentaires qui décrivent une PR ou une tâche ("ajouté pour X", "fix bug Y")
- `# TODO: remove this later` qui restent 6 mois — soit tu le fais maintenant,
  soit tu ouvres un vrai ticket et tu le références.
- `# was: ...`, `# removed: ...`, `# old code below` — utilise git.

### Avant de marquer comme terminé

Quand tu modifies une fonction publique (renommage, signature, retour) :
1. `grep` tous les usages dans le repo (Python + JS + HTML).
2. Mets-les à jour dans la même modif, ou liste explicitement à l'utilisateur
   ce qui reste à migrer.
3. Si tu remplaces une fonction par une nouvelle : supprime l'ancienne, ne laisse
   pas les deux coexister.

### Audit ponctuel

Pour un pass de cleanup global (suffixes historiques, fonctions inutilisées,
fallbacks chaînés) : utiliser le skill `/clean`. Il est explicitement à la demande,
pas automatique.
