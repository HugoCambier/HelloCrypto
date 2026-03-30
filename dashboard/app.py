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

# ── project root on path ──────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

# ── import existing Flask app ─────────────────────────────────────────────────
from hellocrypto.dashboard import app, log  # noqa: E402  (must be after sys.path)
from db.store import add_user, is_user_allowed, list_users, remove_user  # noqa: E402

# ── Jinja: look in dashboard/templates/ first (login page), then templates/ ──
_DASH_TPL = str(Path(__file__).parent / "templates")
app.jinja_loader = ChoiceLoader([
    FileSystemLoader(_DASH_TPL),
    app.jinja_loader,
])

# ── Session secret ────────────────────────────────────────────────────────────
app.secret_key = os.getenv("SESSION_SECRET_KEY", "dev-secret-change-me-in-prod")

# ── OAuth2 credentials ────────────────────────────────────────────────────────
_CLIENT_ID     = os.getenv("GOOGLE_CLIENT_ID", "")
_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
_AUTH_ENABLED  = bool(_CLIENT_ID and _CLIENT_SECRET)

if not _AUTH_ENABLED:
    log.warning("GOOGLE_CLIENT_ID / GOOGLE_CLIENT_SECRET manquants — auth désactivée (mode local)")

# Allow http:// for local dev (oauthlib requirement)
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
    public = ("/login", "/callback", "/healthz")
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
    session["oauth_state"] = state
    return redirect(auth_url)


@app.get("/callback")
def callback():
    from google.oauth2 import id_token  # type: ignore
    from google.auth.transport import requests as google_requests  # type: ignore
    try:
        flow = _oauth_flow()
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


# ── Cloud Run Job control ─────────────────────────────────────────────────────

_GCP_PROJECT  = os.getenv("GOOGLE_CLOUD_PROJECT")
_GCP_REGION   = os.getenv("GCP_REGION", "europe-west9")
_RUNNER_JOB   = os.getenv("RUNNER_JOB", "hellocrypto-runner")
_SCHEDULER_JOB = os.getenv("SCHEDULER_JOB", "hellocrypto-trigger")


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
        return jsonify({"cloud_run": True, "error": str(exc)}), 500


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
        return jsonify({"error": str(exc)}), 500


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
        return jsonify({"error": str(exc)}), 500


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
                 f"/locations/{_GCP_REGION}/jobs/{_SCHEDULER_JOB}")
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
        return jsonify({"error": str(exc)}), 500


# ── User management API ───────────────────────────────────────────────────────

@app.get("/api/users")
def users_list():
    from flask import jsonify
    return jsonify(list_users())


@app.post("/api/users")
def users_add():
    from flask import jsonify
    body  = request.json or {}
    email = body.get("email", "").strip().lower()
    role  = body.get("role", "viewer")
    if not email or "@" not in email:
        return jsonify({"error": "email invalide"}), 400
    add_user(email, role)
    return jsonify({"ok": True, "email": email})


@app.delete("/api/users/<path:email>")
def users_remove(email: str):
    from flask import jsonify
    remove_user(email)
    return jsonify({"ok": True})


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    import db.store as store
    store.init_db()
    from pathlib import Path
    Path("logs").mkdir(exist_ok=True)
    Path("data").mkdir(exist_ok=True)
    host = os.getenv("FLASK_HOST", "127.0.0.1")
    port = int(os.getenv("FLASK_PORT", "5000"))
    print(f"Dashboard → http://{host}:{port}")
    app.run(host=host, port=port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
