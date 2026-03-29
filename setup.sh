#!/usr/bin/env bash
# HelloCrypto — installation automatique sur Compute Engine (Debian/Ubuntu)
# Usage : bash setup.sh
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

WORK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$WORK_DIR"

GRN='\033[0;32m'; YEL='\033[1;33m'; RED='\033[0;31m'; BLD='\033[1m'; NC='\033[0m'
ok()   { echo -e "${GRN}✓${NC} $1"; }
warn() { echo -e "${YEL}⚠${NC}  $1"; }
err()  { echo -e "${RED}✗ ERREUR${NC} $1"; exit 1; }
step() { echo -e "\n${BLD}── $1 ──${NC}"; }

echo -e "${BLD}═══ HelloCrypto — setup GCP ═══${NC}"
echo "Répertoire : $WORK_DIR"
echo "Utilisateur : $(whoami)"

# ── 1. Paquets système ────────────────────────────────────────────────────────
step "Paquets système"
sudo apt-get update -qq
sudo apt-get install -y -qq python3.11 python3.11-venv python3-pip curl git
ok "Python $(python3.11 --version) installé"

# ── 2. Poetry ─────────────────────────────────────────────────────────────────
step "Poetry"
if ! command -v poetry &>/dev/null && [ ! -x "$HOME/.local/bin/poetry" ]; then
    curl -sSL https://install.python-poetry.org | python3.11 -
    ok "Poetry installé"
else
    ok "Poetry déjà présent"
fi
export PATH="$HOME/.local/bin:$PATH"
# Persistant pour les prochaines sessions
grep -qxF 'export PATH="$HOME/.local/bin:$PATH"' ~/.bashrc \
    || echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc

POETRY_BIN="$(command -v poetry)"

# ── 3. Dépendances Python ─────────────────────────────────────────────────────
step "Dépendances Python"
"$POETRY_BIN" install --extras gemini --no-interaction
ok "Dépendances installées (venv Poetry)"

# ── 4. Répertoires runtime ────────────────────────────────────────────────────
step "Répertoires"
mkdir -p "$WORK_DIR/data" "$WORK_DIR/logs"
ok "data/ et logs/ créés"

# ── 5. Fichier .env ───────────────────────────────────────────────────────────
step "Configuration secrets"
if [ ! -f "$WORK_DIR/.env" ]; then
    cp "$WORK_DIR/.env.example" "$WORK_DIR/.env"
    warn "Fichier .env créé — REMPLIS LES CLÉS API avant de démarrer !"
    warn "  nano $WORK_DIR/.env"
else
    ok "Fichier .env existant conservé"
fi

# ── 6. Services systemd ───────────────────────────────────────────────────────
step "Services systemd"
USER_NAME="$(whoami)"

# Service : agent de trading (boucle infinie, redémarre automatiquement)
sudo tee /etc/systemd/system/hellocrypto-agent.service > /dev/null << UNIT
[Unit]
Description=HelloCrypto Trading Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${WORK_DIR}
EnvironmentFile=${WORK_DIR}/.env
ExecStart=${POETRY_BIN} run agent
Restart=on-failure
RestartSec=30
StandardOutput=append:${WORK_DIR}/logs/agent.log
StandardError=append:${WORK_DIR}/logs/agent.log

[Install]
WantedBy=multi-user.target
UNIT

# Service : dashboard web (Flask sur port 5000)
sudo tee /etc/systemd/system/hellocrypto-dashboard.service > /dev/null << UNIT
[Unit]
Description=HelloCrypto Dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USER_NAME}
WorkingDirectory=${WORK_DIR}
EnvironmentFile=${WORK_DIR}/.env
ExecStart=${POETRY_BIN} run dashboard
Restart=on-failure
RestartSec=10
StandardOutput=append:${WORK_DIR}/logs/dashboard.log
StandardError=append:${WORK_DIR}/logs/dashboard.log

[Install]
WantedBy=multi-user.target
UNIT

sudo systemctl daemon-reload
sudo systemctl enable hellocrypto-agent hellocrypto-dashboard
ok "Services installés et activés au démarrage"

# ── 7. Rotation des logs ──────────────────────────────────────────────────────
step "Logrotate"
sudo tee /etc/logrotate.d/hellocrypto > /dev/null << CONF
${WORK_DIR}/logs/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
    copytruncate
}
CONF
ok "Rotation automatique (14 jours)"

# ── Résumé ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLD}═══ Installation terminée ═══${NC}"
echo ""
echo -e "${YEL}Prochaines étapes :${NC}"
echo ""
echo -e "  ${BLD}1. Configurer les secrets${NC}"
echo "     nano $WORK_DIR/.env"
echo "     (BINANCE_API_KEY, BINANCE_API_SECRET, GEMINI_API_KEY)"
echo ""
echo -e "  ${BLD}2. Vérifier config.json${NC} (budget, watchlist, LLM...)"
echo "     nano $WORK_DIR/config.json"
echo ""
echo -e "  ${BLD}3. Démarrer l'agent${NC}"
echo "     sudo systemctl start hellocrypto-agent"
echo "     sudo systemctl start hellocrypto-dashboard"
echo ""
echo -e "  ${BLD}4. Suivre les logs${NC}"
echo "     journalctl -u hellocrypto-agent -f"
echo "     tail -f $WORK_DIR/logs/agent.log"
echo ""
echo -e "  ${BLD}5. Dashboard web${NC}"
echo "     http://EXTERNAL_IP:5000"
echo "     (ouvre le port 5000 dans les règles de pare-feu GCP)"
echo ""
echo -e "${YEL}Cloud Scheduler (optionnel — économie de coûts) :${NC}"
echo "  Arrêt auto la nuit :"
echo "    gcloud scheduler jobs create http stop-vm \\"
echo "      --schedule='0 2 * * *' --time-zone='Europe/Paris' \\"
echo "      --uri='https://compute.googleapis.com/compute/v1/projects/PROJECT/zones/ZONE/instances/INSTANCE/stop' \\"
echo "      --oauth-service-account-email=SA@PROJECT.iam.gserviceaccount.com"
echo "  Redémarrage le matin :"
echo "    (remplace 'stop' par 'start' et '0 2' par '8 0')"
