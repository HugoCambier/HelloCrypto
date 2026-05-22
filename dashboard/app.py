"""HelloCrypto Dashboard.

Wraps hellocrypto.dashboard (all existing routes) and adds:
  - Google OAuth2 authentication (whitelist via ALLOWED_EMAILS)
  - User management API
"""
import os
import sys
from functools import wraps
from pathlib import Path

from flask import redirect, render_template, request, session, url_for
from jinja2 import ChoiceLoader, FileSystemLoader
from werkzeug.middleware.proxy_fix import ProxyFix

# ── project root on path ──────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv
load_dotenv()

# ── import existing Flask app ─────────────────────────────────────────────────
from hellocrypto.dashboard import app, log  # noqa: E402  (must be after sys.path)
from db.store import init_db, is_user_allowed, sync_users_from_env  # noqa: E402

# Ensure DB schema exists at module import (Vercel doesn't call main()).
_INIT_DB_ERROR: str | None = None
try:
    init_db()
except Exception as _exc:
    _INIT_DB_ERROR = f"{type(_exc).__name__}: {_exc}"
    log.exception("init_db() failed at module load")

# Trust X-Forwarded-Proto so request.url uses https://
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

# ── Jinja: look in dashboard/templates/ first (login page), then templates/ ──
_DASH_TPL = str(Path(__file__).parent / "templates")
app.jinja_loader = ChoiceLoader([
    FileSystemLoader(_DASH_TPL),
    app.jinja_loader,
])

# ── Session secret ────────────────────────────────────────────────────────────
_session_secret = os.getenv("SESSION_SECRET_KEY")
_is_production  = bool(os.getenv("K_SERVICE") or os.getenv("GOOGLE_CLOUD_PROJECT") or os.getenv("RENDER"))
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
    if not request.args.get("code"):
        return redirect(url_for("login"))
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


@app.get("/debug/health")
def debug_health():
    """Public diagnostics — no secrets exposed, only presence of env vars."""
    from flask import jsonify
    db_status: str
    db_error: str | None = None
    try:
        from db.store import _USE_POSTGRES, _postgres  # type: ignore
        if _USE_POSTGRES:
            with _postgres() as c:
                c.execute("SELECT 1")
            db_status = "ok (postgres)"
        else:
            db_status = "ok (sqlite)"
    except Exception as exc:
        db_status = "error"
        db_error = f"{type(exc).__name__}: {exc}"
    binance_status: str
    binance_error: str | None = None
    try:
        from hellocrypto.api import get_balance  # type: ignore
        usdc = get_balance("USDC")
        binance_status = f"ok (USDC balance read: {usdc:.4f})"
    except Exception as exc:
        binance_status = "error"
        binance_error = f"{type(exc).__name__}: {exc}"
    return jsonify({
        "init_db_error_at_boot": _INIT_DB_ERROR,
        "db_runtime_check": db_status,
        "db_runtime_error": db_error,
        "binance_check": binance_status,
        "binance_error": binance_error,
        "auth_enabled": _AUTH_ENABLED,
        "client_id_set": bool(_CLIENT_ID),
        "client_secret_set": bool(_CLIENT_SECRET),
        "session_secret_set": bool(_session_secret) and _session_secret != "dev-secret-change-me-in-prod",
        "database_url_set": bool(os.getenv("DATABASE_URL")),
        "allowed_emails_set": bool(os.getenv("ALLOWED_EMAILS")),
        "binance_api_key_set": bool(os.getenv("BINANCE_API_KEY")),
        "binance_api_secret_set": bool(os.getenv("BINANCE_API_SECRET")),
        "gemini_api_key_set": bool(os.getenv("GEMINI_API_KEY")),
        "anthropic_api_key_set": bool(os.getenv("ANTHROPIC_API_KEY")),
        "redirect_uri_would_be": request.url_root.rstrip("/") + "/callback",
        "request_scheme": request.scheme,
        "request_host": request.host,
    })


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
