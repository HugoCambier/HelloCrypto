# HelloCrypto — Claude project rules

## Security

**Ne jamais installer une librairie publiée depuis moins de 24 heures.**

Avant tout `pip install` d'un nouveau paquet, vérifier sa date de première publication sur PyPI
(via `pip index versions <package>` ou `https://pypi.org/pypi/<package>/json`).
Si le paquet a été publié il y a moins de 1 jour, refuser l'installation et en informer l'utilisateur.

Cette règle vise à éviter les attaques par typosquatting et les compromissions de chaîne
d'approvisionnement (supply chain attacks) via des paquets malveillants fraîchement publiés.
