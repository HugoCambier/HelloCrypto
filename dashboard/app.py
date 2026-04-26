"""HelloCrypto Dashboard — Cloud Run Service.

Wraps hellocrypto.dashboard (all existing routes) and adds:
  - Google OAuth2 authentication (whitelist via Firestore / ALLOWED_EMAILS)
  - Cloud Run Job control (start / stop / status)
  - User management API
"""
import os
import sys
from functools import wraps
from pathlib import Path

import requests as _req
from flask import redirect, render_template, request, session, url_for
from jinja2 import ChoiceLoader, FileSystemLoader
from werkzeug.middleware.proxy_fix import ProxyFix

# ── project root on path ──────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

# ── import existing Flask app ─────────────────────────────────────────────────
from hellocrypto.dashboard import app, log  # noqa: E402  (must be after sys.path)
from db.store import is_user_allowed, sync_users_from_env  # noqa: E402

# Trust Cloud Run's X-Forwarded-Proto so request.url uses https://
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ── Jinja: look in dashboard/templates/ first (login page), then templates/ ──
_DASH_TPL = str(Path(__file__).parent / "templates")
app.jinja_loader = ChoiceLoader([
    FileSystemLoader(_DASH_TPL),
    app.jinja_loader,
])

# ── Session secret ────────────────────────────────────────────────────────────
_session_secret = os.getenv("SESSION_SECRET_KEY")
_is_production  = bool(os.getenv("K_SERVICE") or os.getenv("GOOGLE_CLOUD_PROJECT"))
if not _session_secret:
    if _is_production:
        raise RuntimeError(
            "SESSION_SECRET_KEY is required in production. "
            "Set it as an environment variable before starting the app."
        )
    log.warning("SESSION_SECRET_KEY non défini — utilisation d'une clé de dev (NON SÉCURISÉ)")
    _session_secret = "dev-secret-change-me-in-prod"
app.secret_key = _session_secret
app.config.update(
    SESSION_COOKIE_SAMESITE="None",
    SESSION_COOKIE_SECURE=True,   # requis avec SameSite=None (HTTPS uniquement)
    SESSION_COOKIE_HTTPONLY=True,
)

# ── OAuth2 credentials ────────────────────────────────────────────────────────
_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
_AUTH_ENABLED  = bool(_CLIENT_ID and _CLIENT_SECRET)

if not _AUTH_ENABLED:
    log.warning("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET manquants — auth désactivée (mode local)")

# Allow http:// for local dev only (oauthlib requirement)
if not _AUTH_ENABLED:
    os.environ.setdefault("OAUTHLIB_INSECURE_TRANSPORT", "1")

# ── Auth helpers ──────────────────────────────────────────────────────────────

def _oauth_flow():
    from google_auth_oauthlib.flow import Flow  # type: ignore
    return Flow.from_client_config(
        {"web": {
            "client_id":     _CLIENT_ID,
            "client_secret": _CLIENT_SECRET,
            "auth_uri":  "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
        }},
        scopes=[
            "openid",
            "https://www.googleapis.com/auth/userinfo.email",
            "https://www.googleapis.com/auth/userinfo.profile",
        ],
        redirect_uri=request.url_root.rstrip("/") + "/callback",
    )


def _gcp_token() -> str:
    """Get a short-lived GCP identity token (works on Cloud Run via metadata server)."""
    import google.auth  # type: ignore
    import google.auth.transport.requests  # type: ignore
    creds, _ = google.auth.default(
        scopes=["https://www.googleapis.com/auth/cloud-platform"]
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def require_login(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if _AUTH_ENABLED and not session.get("user"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return wrapper



# ── Auth gate (applied to all existing routes) ────────────────────────────────

@app.before_request
def _auth_gate():
    if not _AUTH_ENABLED:
        return  # Auth disabled locally
    public = ("/login", "/auth/", "/callback", "/healthz", "/debug/",
               "/api/simulation/keepalive")
    if request.path.startswith(public):
        return
    if not session.get("user"):
        return redirect(url_for("login"))


# ── Auth routes ───────────────────────────────────────────────────────────────

@app.get("/login")
def login():
    if session.get("user"):
        return redirect("/")
    return render_template("login.html", auth_enabled=_AUTH_ENABLED)


@app.get("/auth/start")
def auth_start():
    flow = _oauth_flow()
    auth_url, state = flow.authorization_url(prompt="select_account")
    session["oauth_state"]         = state
    session["oauth_code_verifier"] = flow.code_verifier  # needed for PKCE in callback
    return redirect(auth_url)


@app.get("/callback")
def callback():
    from google.oauth2 import id_token  # type: ignore
    from google.auth.transport import requests as google_requests  # type: ignore
    try:
        flow = _oauth_flow()
        flow.code_verifier = session.pop("oauth_code_verifier", None)
        flow.fetch_token(authorization_response=request.url)
        credentials = flow.credentials
        id_info = id_token.verify_oauth2_token(
            credentials.id_token,
            google_requests.Request(),
            _CLIENT_ID,
        )
        email = id_info["email"]
        if not is_user_allowed(email):
            return render_template("login.html", error=f"{email} n'est pas autorisé.", auth_enabled=True), 403
        session["user"] = {"email": email, "name": id_info.get("name", email)}
        log.info("Connexion : %s", email)
        return redirect("/")
    except Exception as exc:
        log.error("Erreur callback OAuth2 : %s", exc)
        return render_template("login.html", error="Erreur d'authentification.", auth_enabled=True), 500


@app.get("/logout")
def logout():
    user = session.get("user", {})
    log.info("Déconnexion : %s", user.get("email", "?"))
    session.clear()
    return redirect(url_for("login"))


@app.get("/healthz")
def healthz():
    from flask import jsonify
    return jsonify({"ok": True})


@app.get("/debug/auth")
@require_login
def debug_auth():
    """Check auth config — for troubleshooting (admin only)."""
    from flask import jsonify
    if _AUTH_ENABLED and session.get("user", {}).get("role") != "admin":
        return jsonify({"error": "Accès refusé"}), 403
    return jsonify({
        "auth_enabled": _AUTH_ENABLED,
        "client_id_set": bool(_CLIENT_ID),
        "client_secret_set": bool(_CLIENT_SECRET),
        "redirect_uri_would_be": request.url_root.rstrip("/") + "/callback",
    })


# ── Cloud Run Job control ─────────────────────────────────────────────────────

_GCP_PROJECT     = os.getenv("GOOGLE_CLOUD_PROJECT")
_GCP_REGION      = os.getenv("GCP_REGION", "europe-west9")
_SCHEDULER_REGION = os.getenv("SCHEDULER_REGION", "europe-west1")
_RUNNER_JOB      = os.getenv("RUNNER_JOB", "hellocrypto-runner")
_SCHEDULER_JOB   = os.getenv("SCHEDULER_JOB", "hellocrypto-trigger")


@app.get("/api/runner/status")
def runner_status():
    from flask import jsonify
    if not _GCP_PROJECT:
        return jsonify({"cloud_run": False, "message": "GCP_PROJECT non configuré (mode local)"})
    try:
        token  = _gcp_token()
        url    = (f"https://run.googleapis.com/v2/projects/{_GCP_PROJECT}"
                  f"/locations/{_GCP_REGION}/jobs/{_RUNNER_JOB}/executions")
        r      = _req.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=8)
        r.raise_for_status()
        execs  = r.json().get("executions", [])
        latest = execs[0] if execs else None
        running = bool(latest and latest.get("completionTime") is None)
        return jsonify({"cloud_run": True, "running": running, "latest": latest})
    except Exception as exc:
        log.exception("Erreur runner_status")
        return jsonify({"cloud_run": True, "error": "Erreur interne du serveur"}), 500


@app.post("/api/runner/start")
def runner_start():
    from flask import jsonify
    if not _GCP_PROJECT:
        return jsonify({"error": "GCP_PROJECT non configuré"}), 400
    body = request.json or {}
    mode = body.get("mode", "real")
    try:
        token = _gcp_token()
        url   = (f"https://run.googleapis.com/v2/projects/{_GCP_PROJECT}"
                 f"/locations/{_GCP_REGION}/jobs/{_RUNNER_JOB}:run")
        r = _req.post(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={"overrides": {"containerOverrides": [{"args": [f"--mode={mode}"]}]}},
            timeout=10,
        )
        r.raise_for_status()
        return jsonify({"ok": True, "execution": r.json().get("name")})
    except Exception as exc:
        log.exception("Erreur runner_start")
        return jsonify({"error": "Erreur interne du serveur"}), 500


@app.post("/api/runner/stop")
def runner_stop():
    from flask import jsonify
    if not _GCP_PROJECT:
        return jsonify({"error": "GCP_PROJECT non configuré"}), 400
    try:
        token = _gcp_token()
        # Get latest execution
        list_url = (f"https://run.googleapis.com/v2/projects/{_GCP_PROJECT}"
                    f"/locations/{_GCP_REGION}/jobs/{_RUNNER_JOB}/executions?pageSize=1")
        execs = _req.get(list_url, headers={"Authorization": f"Bearer {token}"}, timeout=8).json()
        latest = (execs.get("executions") or [{}])[0]
        name = latest.get("name")
        if not name:
            return jsonify({"ok": True, "message": "Aucune exécution en cours"})
        cancel_url = f"https://run.googleapis.com/v2/{name}:cancel"
        _req.post(cancel_url, headers={"Authorization": f"Bearer {token}"}, timeout=10).raise_for_status()
        return jsonify({"ok": True})
    except Exception as exc:
        log.exception("Erreur runner_stop")
        return jsonify({"error": "Erreur interne du serveur"}), 500


def _scheduler_action(action: str):
    """Pause or resume the Cloud Scheduler job. action = 'pause' | 'resume'."""
    from flask import jsonify
    if not _GCP_PROJECT:
        return jsonify({"error": "GCP_PROJECT non configuré"}), 400
    try:
        token = _gcp_token()
        url   = (f"https://cloudscheduler.googleapis.com/v1/projects/{_GCP_PROJECT}"
                 f"/locations/{_SCHEDULER_REGION}/jobs/{_SCHEDULER_JOB}:{action}")
        r = _req.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=10)
        r.raise_for_status()
        return jsonify({"ok": True, "action": action})
    except Exception as exc:
        log.exception("Erreur scheduler_action %s", action)
        return jsonify({"error": "Erreur interne du serveur"}), 500


@app.get("/api/runner/schedule")
def runner_schedule_status():
    """Return scheduler state: enabled (resumed) or disabled (paused)."""
    from flask import jsonify
    if not _GCP_PROJECT:
        return jsonify({"cloud_run": False, "enabled": False})
    try:
        token = _gcp_token()
        url   = (f"https://cloudscheduler.googleapis.com/v1/projects/{_GCP_PROJECT}"
                 f"/locations/{_SCHEDULER_REGION}/jobs/{_SCHEDULER_JOB}")
        r = _req.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=8)
        r.raise_for_status()
        state   = r.json().get("state", "")
        enabled = state == "ENABLED"
        schedule = r.json().get("schedule", "")
        return jsonify({"cloud_run": True, "enabled": enabled, "schedule": schedule})
    except Exception as exc:
        log.exception("Erreur runner_schedule_status")
        return jsonify({"cloud_run": True, "enabled": False, "error": "Erreur interne"})


@app.post("/api/runner/schedule/enable")
def runner_schedule_enable():
    return _scheduler_action("resume")


@app.post("/api/runner/schedule/disable")
def runner_schedule_disable():
    return _scheduler_action("pause")


@app.post("/api/runner/frequency")
def runner_frequency():
    """Update Cloud Scheduler interval (minimum 1 minute)."""
    from flask import jsonify
    if not _GCP_PROJECT:
        return jsonify({"error": "GCP_PROJECT non configuré"}), 400
    seconds = max(60, int((request.json or {}).get("seconds", 60)))
    minutes = seconds // 60
    cron    = f"*/{minutes} * * * *"
    try:
        token = _gcp_token()
        url   = (f"https://cloudscheduler.googleapis.com/v1/projects/{_GCP_PROJECT}"
                 f"/locations/{_SCHEDULER_REGION}/jobs/{_SCHEDULER_JOB}")
        r = _req.patch(
            url,
            headers={"Authorization": f"Bearer {token}"},
            json={"schedule": cron},
            params={"updateMask": "schedule"},
            timeout=10,
        )
        r.raise_for_status()
        return jsonify({"ok": True, "cron": cron, "minutes": minutes})
    except Exception as exc:
        log.exception("Erreur runner_frequency")
        return jsonify({"error": "Erreur interne du serveur"}), 500


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    import logging as _logging
    import db.store as store
    # Ensure INFO-level logs from the simulation/agent threads reach DBLogHandler
    _logging.basicConfig(level=_logging.INFO,
                         format="%(asctime)s [%(levelname)s] %(name)s — %(message)s")
    _logging.getLogger().setLevel(_logging.INFO)
    store.init_db()
    sync_users_from_env()
    from pathlib import Path
    Path("logs").mkdir(exist_ok=True)
    Path("data").mkdir(exist_ok=True)
    # Cloud Run sets PORT; FLASK_HOST defaults to 0.0.0.0 (required by Cloud Run).
    # On a VM behind nginx, set FLASK_HOST=127.0.0.1 in the systemd service.
    host = os.getenv("FLASK_HOST", "0.0.0.0")
    port = int(os.getenv("PORT", os.getenv("FLASK_PORT", "5000")))
    print(f"Dashboard → http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
