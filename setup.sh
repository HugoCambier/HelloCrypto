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

# ── 0. Paramètres SSL ─────────────────────────────────────────────────────────
echo ""
echo -e "${YEL}Configuration HTTPS (optionnel)${NC}"
echo "  Pour un certificat SSL gratuit (Let's Encrypt), tu as besoin d'un domaine"
echo "  pointant sur cette VM. Ex: DuckDNS (gratuit) → monapp.duckdns.org"
echo ""
read -rp "Domaine (laisser vide pour ignorer SSL) : " DOMAIN
if [ -n "$DOMAIN" ]; then
    read -rp "Email (pour Let's Encrypt) : " LE_EMAIL
fi

# ── 1. Paquets système ────────────────────────────────────────────────────────
step "Paquets système"
sudo apt-get update -qq
sudo apt-get install -y -qq python3.11 python3.11-venv python3-pip curl git nginx
ok "Python $(python3.11 --version) + nginx installés"

# ── 2. Poetry ─────────────────────────────────────────────────────────────────
step "Poetry"
if ! command -v poetry &>/dev/null && [ ! -x "$HOME/.local/bin/poetry" ]; then
    curl -sSL https://install.python-poetry.org | python3.11 -
    ok "Poetry installé"
else
    ok "Poetry déjà présent"
fi
export PATH="$HOME/.local/bin:$PATH"
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

# Service : agent de trading
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

# Service : dashboard web (Flask sur 127.0.0.1:5000 — nginx expose vers l'extérieur)
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
Environment=FLASK_HOST=127.0.0.1
Environment=FLASK_PORT=5000
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

# ── 8. nginx — reverse proxy ──────────────────────────────────────────────────
step "nginx"

if [ -n "${DOMAIN:-}" ]; then
    SERVER_NAME="$DOMAIN"
else
    # Utilise l'IP publique GCP si disponible, sinon localhost
    SERVER_NAME="$(curl -sf --max-time 2 http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/externalIp -H 'Metadata-Flavor: Google' || echo '_')"
fi

sudo tee /etc/nginx/sites-available/hellocrypto > /dev/null << NGINX
server {
    listen 80;
    server_name ${SERVER_NAME};

    # Taille max upload (backtest fichiers)
    client_max_body_size 10M;

    location / {
        proxy_pass         http://127.0.0.1:5000;
        proxy_set_header   Host              \$host;
        proxy_set_header   X-Real-IP         \$remote_addr;
        proxy_set_header   X-Forwarded-For   \$proxy_add_x_forwarded_for;
        proxy_set_header   X-Forwarded-Proto \$scheme;
        proxy_read_timeout 120s;

        # WebSocket (simulation live)
        proxy_http_version 1.1;
        proxy_set_header   Upgrade    \$http_upgrade;
        proxy_set_header   Connection "upgrade";
    }
}
NGINX

# Activer le site, désactiver le défaut
sudo ln -sf /etc/nginx/sites-available/hellocrypto /etc/nginx/sites-enabled/hellocrypto
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t && sudo systemctl enable nginx && sudo systemctl restart nginx
ok "nginx configuré (reverse proxy → 127.0.0.1:5000)"

# ── 9. SSL — Let's Encrypt (certbot) ─────────────────────────────────────────
if [ -n "${DOMAIN:-}" ]; then
    step "SSL — Let's Encrypt pour $DOMAIN"

    # Installe certbot via snap (méthode recommandée sur Ubuntu/Debian)
    if ! command -v certbot &>/dev/null; then
        sudo apt-get install -y -qq snapd
        sudo snap install --classic certbot 2>/dev/null || sudo apt-get install -y -qq certbot python3-certbot-nginx
        ok "certbot installé"
    else
        ok "certbot déjà présent"
    fi

    sudo certbot --nginx \
        -d "$DOMAIN" \
        --non-interactive \
        --agree-tos \
        --email "$LE_EMAIL" \
        --redirect
    ok "Certificat SSL obtenu — HTTPS activé pour $DOMAIN"
    ok "Renouvellement automatique via systemd timer certbot"

    # Redémarre nginx après certbot (certbot le fait aussi, mais au cas où)
    sudo systemctl reload nginx

    DASHBOARD_URL="https://$DOMAIN"
else
    warn "Pas de domaine fourni — dashboard accessible en HTTP sur le port 80"
    warn "Pour activer HTTPS plus tard : relance setup.sh avec un domaine"
    EXTERNAL_IP="$(curl -sf --max-time 2 http://metadata.google.internal/computeMetadata/v1/instance/network-interfaces/0/access-configs/0/externalIp -H 'Metadata-Flavor: Google' || echo 'EXTERNAL_IP')"
    DASHBOARD_URL="http://$EXTERNAL_IP"
fi

# ── Résumé ─────────────────────────────────────────────────────────────────────
echo ""
echo -e "${BLD}═══ Installation terminée ═══${NC}"
echo ""
echo -e "${YEL}Prochaines étapes :${NC}"
echo ""
echo -e "  ${BLD}1. Configurer les secrets${NC}"
echo "     nano $WORK_DIR/.env"
echo "     (BINANCE_API_KEY, BINANCE_API_SECRET, GEMINI_API_KEY)"
if [ -n "${LE_EMAIL:-}" ]; then
    echo "     (GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET pour l'auth Google)"
fi
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
echo "     $DASHBOARD_URL"
echo "     (assure-toi que le port 80/443 est ouvert dans les règles de pare-feu GCP)"
if [ -n "${DOMAIN:-}" ]; then
    echo ""
    echo -e "  ${BLD}6. OAuth2 Google — callback URI à enregistrer${NC}"
    echo "     https://console.cloud.google.com/apis/credentials"
    echo "     Ajoute : https://$DOMAIN/callback"
fi
echo ""
echo -e "${YEL}Pare-feu GCP (si pas déjà fait) :${NC}"
echo "  gcloud compute firewall-rules create allow-http-https \\"
echo "    --allow tcp:80,tcp:443 --target-tags=http-server,https-server"
echo ""
echo -e "${YEL}Arrêt/démarrage automatique (économie de coûts) :${NC}"
echo "  Arrêt la nuit (2h) :"
echo "    gcloud scheduler jobs create http stop-vm \\"
echo "      --schedule='0 2 * * *' --time-zone='Europe/Paris' \\"
echo "      --uri='https://compute.googleapis.com/compute/v1/projects/PROJECT/zones/ZONE/instances/INSTANCE/stop' \\"
echo "      --oauth-service-account-email=SA@PROJECT.iam.gserviceaccount.com"
echo "  Redémarrage le matin (8h) :"
echo "    (remplace 'stop' par 'start' et '0 2' par '0 8')"
