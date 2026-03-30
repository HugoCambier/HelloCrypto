#!/usr/bin/env bash
# HelloCrypto — déploiement GCP (Cloud Run + Firestore + Scheduler)
#
# Usage:
#   bash deploy/deploy.sh
#
# Prérequis:
#   - gcloud CLI installé et authentifié (gcloud auth login)
#   - Docker installé et en cours d'exécution
#   - Fichier .env avec les secrets (BINANCE_API_KEY, etc.)
#   - OAuth2 credentials créés dans GCP Console (voir README)
#
set -euo pipefail

# ── Configuration ─────────────────────────────────────────────────────────────
PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null)}"
REGION="${GCP_REGION:-europe-west9}"
REPO="hellocrypto"
RUNNER_IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/runner"
DASHBOARD_IMAGE="$REGION-docker.pkg.dev/$PROJECT/$REPO/dashboard"
RUNNER_JOB="hellocrypto-runner"
DASHBOARD_SVC="hellocrypto-dashboard"
SCHEDULER_JOB="hellocrypto-trigger"
SA="hellocrypto-sa"

# ── Colours ───────────────────────────────────────────────────────────────────
ok()   { echo -e "\033[0;32m✓\033[0m  $1"; }
warn() { echo -e "\033[1;33m⚠\033[0m  $1"; }
step() { echo -e "\n\033[1m── $1 ──\033[0m"; }
die()  { echo -e "\033[0;31m✗\033[0m  $1"; exit 1; }

[ -z "$PROJECT" ] && die "Projet GCP non trouvé. Lance: gcloud config set project TON_PROJET"

echo -e "\033[1m═══ HelloCrypto — déploiement GCP ═══\033[0m"
echo "Projet : $PROJECT  |  Région : $REGION"

# ── 1. APIs ───────────────────────────────────────────────────────────────────
step "Activation des APIs GCP"
gcloud services enable \
    run.googleapis.com \
    cloudbuild.googleapis.com \
    cloudscheduler.googleapis.com \
    firestore.googleapis.com \
    secretmanager.googleapis.com \
    artifactregistry.googleapis.com \
    --project="$PROJECT" --quiet
ok "APIs activées"

# ── 2. Service Account ────────────────────────────────────────────────────────
step "Service Account"
gcloud iam service-accounts create "$SA" \
    --display-name="HelloCrypto SA" --project="$PROJECT" --quiet 2>/dev/null || true
for role in roles/datastore.user roles/secretmanager.secretAccessor roles/run.invoker; do
    gcloud projects add-iam-policy-binding "$PROJECT" \
        --member="serviceAccount:$SA@$PROJECT.iam.gserviceaccount.com" \
        --role="$role" --condition=None --quiet 2>&1 | grep -v "^Updated\|^bindings\|^etag\|^version\|^  -\|^  role\|^  members" || true
done
ok "Service account prêt ($SA)"

# ── 3. Secrets ────────────────────────────────────────────────────────────────
step "Secret Manager"
[ -f .env ] && { set -a; source .env; set +a; }

_upsert_secret() {
    local name="$1" var="$2"
    local val="${!var:-}"
    if [ -z "$val" ]; then
        warn "Variable $var vide — secret '$name' ignoré. Crée-le manuellement si nécessaire."
        return
    fi
    if gcloud secrets describe "$name" --project="$PROJECT" &>/dev/null; then
        echo -n "$val" | gcloud secrets versions add "$name" --data-file=- --project="$PROJECT" --quiet
    else
        echo -n "$val" | gcloud secrets create "$name" --data-file=- --project="$PROJECT" --quiet
    fi
    ok "Secret '$name' mis à jour"
}

_upsert_secret binance-api-key       BINANCE_API_KEY
_upsert_secret binance-api-secret    BINANCE_API_SECRET
_upsert_secret gemini-api-key        GEMINI_API_KEY
_upsert_secret google-client-id      GOOGLE_CLIENT_ID
_upsert_secret google-client-secret  GOOGLE_CLIENT_SECRET

# Generate a session secret if not set
if ! gcloud secrets describe session-secret-key --project="$PROJECT" &>/dev/null; then
    python3 -c "import secrets; print(secrets.token_hex(32))" \
        | gcloud secrets create session-secret-key --data-file=- --project="$PROJECT" --quiet
    ok "Secret 'session-secret-key' généré automatiquement"
fi

# ── 4. Artifact Registry ──────────────────────────────────────────────────────
step "Artifact Registry"
gcloud artifacts repositories create "$REPO" \
    --repository-format=docker --location="$REGION" \
    --project="$PROJECT" --quiet 2>/dev/null || true
gcloud auth configure-docker "$REGION-docker.pkg.dev" --quiet
ok "Registry : $REGION-docker.pkg.dev/$PROJECT/$REPO"

# ── 5. Build & Push (via Cloud Build — pas de Docker local requis) ────────────
step "Build Docker (Cloud Build)"
gcloud builds submit . \
    --tag="$RUNNER_IMAGE" \
    --dockerfile=runner/Dockerfile \
    --project="$PROJECT" --quiet
gcloud builds submit . \
    --tag="$DASHBOARD_IMAGE" \
    --dockerfile=dashboard/Dockerfile \
    --project="$PROJECT" --quiet
ok "Images publiées"

# ── 6. Firestore ──────────────────────────────────────────────────────────────
step "Firestore (Europe multi-région)"
gcloud firestore databases create \
    --location=eur3 --project="$PROJECT" --quiet 2>/dev/null \
    && ok "Base Firestore créée (eur3)" \
    || ok "Base Firestore déjà existante"

# ── 7. Deploy Dashboard ───────────────────────────────────────────────────────
step "Dashboard (Cloud Run Service)"
_SECRETS="BINANCE_API_KEY=binance-api-key:latest"
_SECRETS="$_SECRETS,BINANCE_API_SECRET=binance-api-secret:latest"
_SECRETS="$_SECRETS,GEMINI_API_KEY=gemini-api-key:latest"
_SECRETS="$_SECRETS,GOOGLE_CLIENT_ID=google-client-id:latest"
_SECRETS="$_SECRETS,GOOGLE_CLIENT_SECRET=google-client-secret:latest"
_SECRETS="$_SECRETS,SESSION_SECRET_KEY=session-secret-key:latest"

gcloud run deploy "$DASHBOARD_SVC" \
    --image="$DASHBOARD_IMAGE" \
    --region="$REGION" \
    --platform=managed \
    --allow-unauthenticated \
    --port=5000 \
    --memory=512Mi \
    --min-instances=0 \
    --max-instances=2 \
    --service-account="$SA@$PROJECT.iam.gserviceaccount.com" \
    --set-env-vars="GOOGLE_CLOUD_PROJECT=$PROJECT,GCP_REGION=$REGION,RUNNER_JOB=$RUNNER_JOB,SCHEDULER_JOB=$SCHEDULER_JOB" \
    --set-secrets="$_SECRETS" \
    --project="$PROJECT" --quiet

DASHBOARD_URL=$(gcloud run services describe "$DASHBOARD_SVC" \
    --region="$REGION" --project="$PROJECT" --format="value(status.url)")
ok "Dashboard → $DASHBOARD_URL"

# ── 8. Runner Job ─────────────────────────────────────────────────────────────
step "Runner (Cloud Run Job)"
_JOB_SECRETS="BINANCE_API_KEY=binance-api-key:latest"
_JOB_SECRETS="$_JOB_SECRETS,BINANCE_API_SECRET=binance-api-secret:latest"
_JOB_SECRETS="$_JOB_SECRETS,GEMINI_API_KEY=gemini-api-key:latest"

gcloud run jobs create "$RUNNER_JOB" \
    --image="$RUNNER_IMAGE" \
    --region="$REGION" \
    --task-timeout=3600 \
    --memory=512Mi \
    --service-account="$SA@$PROJECT.iam.gserviceaccount.com" \
    --set-env-vars="GOOGLE_CLOUD_PROJECT=$PROJECT" \
    --set-secrets="$_JOB_SECRETS" \
    --project="$PROJECT" --quiet 2>/dev/null \
|| gcloud run jobs update "$RUNNER_JOB" \
    --image="$RUNNER_IMAGE" \
    --region="$REGION" \
    --set-env-vars="GOOGLE_CLOUD_PROJECT=$PROJECT" \
    --set-secrets="$_JOB_SECRETS" \
    --project="$PROJECT" --quiet
ok "Runner Job : $RUNNER_JOB"

# ── 9. Cloud Scheduler ────────────────────────────────────────────────────────
step "Cloud Scheduler"
CYCLE_SEC=$(python3 -c "import json; print(json.load(open('config.json')).get('cycle_seconds', 60))")
MINUTES=$(python3 -c "print(max(1, $CYCLE_SEC // 60))")
CRON="*/$MINUTES * * * *"
JOB_URI="https://run.googleapis.com/v2/projects/$PROJECT/locations/$REGION/jobs/$RUNNER_JOB:run"

gcloud scheduler jobs create http "$SCHEDULER_JOB" \
    --location="$REGION" \
    --schedule="$CRON" \
    --uri="$JOB_URI" \
    --message-body='{"overrides":{"containerOverrides":[{"args":["--mode","real"]}]}}' \
    --oauth-service-account-email="$SA@$PROJECT.iam.gserviceaccount.com" \
    --project="$PROJECT" --quiet 2>/dev/null \
|| gcloud scheduler jobs update http "$SCHEDULER_JOB" \
    --location="$REGION" \
    --schedule="$CRON" \
    --uri="$JOB_URI" \
    --project="$PROJECT" --quiet
ok "Scheduler créé : $CRON (toutes les $MINUTES min)"

# Pause immédiatement — le run se déclenche depuis le dashboard
gcloud scheduler jobs pause "$SCHEDULER_JOB" \
    --location="$REGION" --project="$PROJECT" --quiet
ok "Scheduler en pause — démarre les cycles depuis le dashboard"

# ── 10. Ajouter le propriétaire comme utilisateur autorisé ───────────────────
step "Utilisateur admin"
OWNER_EMAIL="${ALLOWED_EMAILS:-$(gcloud config get-value account 2>/dev/null)}"
if [ -n "$OWNER_EMAIL" ]; then
    python3 - <<PYEOF
import sys; sys.path.insert(0,".")
import os; os.environ["GOOGLE_CLOUD_PROJECT"] = "$PROJECT"
from db.store import add_user
add_user("$OWNER_EMAIL", "admin")
print("  → $OWNER_EMAIL ajouté comme admin")
PYEOF
fi

# ── Résumé ────────────────────────────────────────────────────────────────────
echo ""
echo -e "\033[1m═══ Déploiement terminé ═══\033[0m"
echo ""
echo "  Dashboard      : $DASHBOARD_URL"
echo "  Connecte-toi   : $OWNER_EMAIL"
echo ""
echo -e "\033[1;33m⚠  Action manuelle requise (1 seule fois) :\033[0m"
echo "   1. Ouvre : https://console.cloud.google.com/apis/credentials"
echo "   2. Édite ton OAuth2 Client → Authorized redirect URIs"
echo "   3. Ajoute : $DASHBOARD_URL/callback"
echo "   4. Sauvegarde, attends 1-2 min"
echo ""
echo "  Ajouter un utilisateur :"
echo "    python3 -c \"import os; os.environ['GOOGLE_CLOUD_PROJECT']='$PROJECT'; from db.store import add_user; add_user('email@example.com')\""
echo ""
echo "  Mettre à jour (après modif du code) :"
echo "    bash deploy/deploy.sh"
