"""Data store — SQLite (local), PostgreSQL (Supabase/Render) or Firestore (GCP Cloud Run).

Auto-detected:
  - DATABASE_URL starts with postgresql:// or postgres:// → PostgreSQL
  - GOOGLE_CLOUD_PROJECT set (and no DATABASE_URL) → Firestore
  - Otherwise → SQLite (default path: data/hellocrypto.db)
"""
import json
import logging
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

_DATABASE_URL  = os.getenv("DATABASE_URL", "")
_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
_USE_POSTGRES  = _DATABASE_URL.startswith(("postgresql://", "postgres://"))
_USE_FIRESTORE = bool(_CLOUD_PROJECT) and not _DATABASE_URL and not _USE_POSTGRES


# ── SQLite ─────────────────────────────────────────────────────────────────────

def _db_path() -> Path:
    url = os.getenv("DATABASE_URL", "data/hellocrypto.db")
    # Guard: a Postgres URL is not a filesystem path. Without this, the
    # mkdir below would materialise a junk directory tree named after the
    # connection string (leaking the password into a path). If we get here
    # with Postgres configured, the SQLite backend was selected by mistake.
    if url.startswith(("postgresql://", "postgres://")):
        raise RuntimeError(
            "_db_path() called while DATABASE_URL points to Postgres — "
            "the SQLite backend must not be used in this configuration."
        )
    p = Path(url.replace("sqlite:///", ""))
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


@contextmanager
def _sqlite():
    conn = sqlite3.connect(str(_db_path()))
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _init_sqlite() -> None:
    with _sqlite() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS trades (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp    TEXT    NOT NULL,
            action       TEXT    NOT NULL,
            symbol       TEXT,
            amount       REAL,
            qty          REAL,
            price        REAL,
            pnl          REAL,
            fee          REAL,
            fee_asset    TEXT    DEFAULT 'USDC',
            reason       TEXT,
            mode         TEXT    DEFAULT 'real',
            session_id   TEXT,
            session_name TEXT,
            binance_order_id TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS agent_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS logs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp  TEXT    NOT NULL,
            level      TEXT    NOT NULL DEFAULT 'info',
            category   TEXT    NOT NULL DEFAULT 'technical',
            message    TEXT    NOT NULL,
            mode       TEXT    DEFAULT 'real',
            cycle      INTEGER,
            session_id TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id            TEXT PRIMARY KEY,
            name          TEXT,
            mode          TEXT NOT NULL DEFAULT 'simulation',
            created_at    TEXT NOT NULL,
            initial_state TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS market_analyses (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp  TEXT    NOT NULL,
            cycle      INTEGER,
            mode       TEXT    DEFAULT 'real',
            session_id TEXT,
            sentiment  TEXT,
            summary    TEXT,
            analyses   TEXT,
            usage      TEXT,
            reasoning  TEXT
        )""")
        for sql in (
            "CREATE INDEX IF NOT EXISTS idx_logs_ts          ON logs(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_logs_mode_ts     ON logs(mode, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_logs_session     ON logs(session_id)",
            "CREATE INDEX IF NOT EXISTS idx_logs_category    ON logs(category)",
            "CREATE INDEX IF NOT EXISTS idx_trades_ts        ON trades(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_trades_mode_ts   ON trades(mode, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_trades_session   ON trades(session_id)",
            "CREATE INDEX IF NOT EXISTS idx_analyses_ts      ON market_analyses(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_analyses_session ON market_analyses(session_id)",
        ):
            c.execute(sql)


def _migrate_sqlite() -> None:
    """Add new columns to existing tables (migration for older DBs)."""
    with _sqlite() as c:
        for table, col, definition in [
            ("trades",          "session_id",       "TEXT"),
            ("trades",          "session_name",     "TEXT"),
            ("trades",          "binance_order_id", "TEXT"),
            ("logs",            "session_id",       "TEXT"),
            ("sessions",        "initial_state",    "TEXT"),
            ("market_analyses", "usage",            "TEXT"),
            ("market_analyses", "reasoning",        "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            except Exception:
                pass  # Column already exists


# ── PostgreSQL ─────────────────────────────────────────────────────────────────

# Pool partagé pour éviter d'ouvrir/fermer une connexion psycopg2 à chaque
# save_log/save_trade (saturait les 60 slots du free tier Supabase + latence
# handshake à chaque appel). Initialisé paresseusement à la première utilisation.
_PG_POOL = None
_PG_POOL_LOCK = threading.Lock()


def _pg_pool():
    global _PG_POOL
    if _PG_POOL is not None:
        return _PG_POOL
    with _PG_POOL_LOCK:
        if _PG_POOL is None:
            import psycopg2.pool  # type: ignore
            # minconn=1 pour ne pas saturer au cold start serverless.
            # maxconn=5 reste large pour un dashboard solo + cron.
            # TCP keepalives: Supabase coupe les conn idle > ~60s. Les scripts
            # longs (backfill, compute_tiers) ont des gaps de plusieurs minutes
            # entre 2 INSERT — sans keepalives, la conn meurt silencieusement.
            _PG_POOL = psycopg2.pool.ThreadedConnectionPool(
                minconn=1, maxconn=int(os.getenv("PG_POOL_MAX", "5")),
                dsn=_DATABASE_URL,
                keepalives=1,
                keepalives_idle=30,
                keepalives_interval=10,
                keepalives_count=5,
            )
    return _PG_POOL


@contextmanager
def _postgres():
    """Yield a psycopg2 DictCursor backed by the shared pool.

    On connection-level errors (server closed conn, broken socket), discard
    the dead conn instead of returning it to the pool — otherwise the next
    caller inherits the corpse and the failure cascades.
    """
    import psycopg2  # type: ignore
    import psycopg2.extras  # type: ignore
    pool = _pg_pool()
    conn = pool.getconn()
    cur  = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)
    broken = False
    try:
        yield cur
        conn.commit()
    except (psycopg2.OperationalError, psycopg2.InterfaceError):
        broken = True
        raise
    except Exception:
        try:
            conn.rollback()
        except (psycopg2.OperationalError, psycopg2.InterfaceError):
            broken = True
        raise
    finally:
        try:
            cur.close()
        except Exception:
            pass
        try:
            pool.putconn(conn, close=broken)
        except Exception:
            pass


def _init_postgres() -> None:
    with _postgres() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS trades (
            id           SERIAL PRIMARY KEY,
            timestamp    TEXT             NOT NULL,
            action       TEXT             NOT NULL,
            symbol       TEXT,
            amount       DOUBLE PRECISION,
            qty          DOUBLE PRECISION,
            price        DOUBLE PRECISION,
            pnl          DOUBLE PRECISION,
            fee          DOUBLE PRECISION,
            fee_asset    TEXT             DEFAULT 'USDC',
            reason       TEXT,
            mode         TEXT             DEFAULT 'real',
            session_id   TEXT,
            session_name TEXT,
            binance_order_id TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS agent_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS logs (
            id         SERIAL PRIMARY KEY,
            timestamp  TEXT    NOT NULL,
            level      TEXT    NOT NULL DEFAULT 'info',
            category   TEXT    NOT NULL DEFAULT 'technical',
            message    TEXT    NOT NULL,
            mode       TEXT    DEFAULT 'real',
            cycle      INTEGER,
            session_id TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS sessions (
            id            TEXT PRIMARY KEY,
            name          TEXT,
            mode          TEXT NOT NULL DEFAULT 'simulation',
            created_at    TEXT NOT NULL,
            initial_state TEXT
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS market_analyses (
            id         SERIAL PRIMARY KEY,
            timestamp  TEXT    NOT NULL,
            cycle      INTEGER,
            mode       TEXT    DEFAULT 'real',
            session_id TEXT,
            sentiment  TEXT,
            summary    TEXT,
            analyses   TEXT,
            usage      TEXT,
            reasoning  TEXT
        )""")
        for sql in (
            "CREATE INDEX IF NOT EXISTS idx_logs_ts          ON logs(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_logs_mode_ts     ON logs(mode, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_logs_session     ON logs(session_id)",
            "CREATE INDEX IF NOT EXISTS idx_logs_category    ON logs(category)",
            "CREATE INDEX IF NOT EXISTS idx_trades_ts        ON trades(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_trades_mode_ts   ON trades(mode, timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_trades_session   ON trades(session_id)",
            "CREATE INDEX IF NOT EXISTS idx_analyses_ts      ON market_analyses(timestamp)",
            "CREATE INDEX IF NOT EXISTS idx_analyses_session ON market_analyses(session_id)",
        ):
            c.execute(sql)
        # Supabase expose toute table `public` via son API REST (PostgREST),
        # joignable avec la clé anon publique. L'app se connecte en direct
        # (rôle `postgres`, qui bypass RLS), donc on active RLS sans policy :
        # ça ferme l'accès anon/authenticated à l'API sans gêner l'app.
        for table in ("trades", "agent_state", "logs", "sessions", "market_analyses"):
            c.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")


def _migrate_postgres() -> None:
    """Add missing columns to existing tables (idempotent)."""
    with _postgres() as c:
        for table, col, definition in [
            ("trades",          "session_id",       "TEXT"),
            ("trades",          "session_name",     "TEXT"),
            ("trades",          "binance_order_id", "TEXT"),
            ("logs",            "session_id",       "TEXT"),
            ("sessions",        "initial_state",    "TEXT"),
            ("market_analyses", "usage",            "TEXT"),
            ("market_analyses", "reasoning",        "TEXT"),
        ]:
            c.execute(
                f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS {col} {definition}"
            )


# ── Firestore ──────────────────────────────────────────────────────────────────

def _fs():
    from google.cloud import firestore  # type: ignore
    return firestore.Client()


# ── Public API ─────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create tables (SQLite / PostgreSQL) or no-op (Firestore)."""
    if _USE_POSTGRES:
        _init_postgres()
        _migrate_postgres()
    elif not _USE_FIRESTORE:
        _init_sqlite()
        _migrate_sqlite()
    # Price snapshots table — used by the journal/playbook system. Lives in
    # its own module to keep that feature cohesive but shares the same backend.
    from db.snapshots import init_snapshots
    init_snapshots()


def save_trade(
    action: str,
    symbol: str,
    amount: float,
    price: float,
    reason: str,
    fee: float = 0.0,
    fee_asset: str = "USDC",
    qty: float | None = None,
    pnl: float | None = None,
    mode: str = "real",
    session_id: str | None = None,
    session_name: str | None = None,
    binance_order_id: str | None = None,
    timestamp: str | None = None,
) -> None:
    # ``timestamp`` lets callers backfill historical fills with their real Binance
    # execution time; live trades default to now.
    ts = timestamp or datetime.utcnow().isoformat()
    if _USE_FIRESTORE:
        _fs().collection("trades").add(dict(
            timestamp=ts, action=action, symbol=symbol, amount=amount,
            qty=qty, price=price, pnl=pnl, fee=fee,
            fee_asset=fee_asset, reason=reason, mode=mode,
            session_id=session_id, session_name=session_name,
            binance_order_id=binance_order_id,
        ))
    elif _USE_POSTGRES:
        with _postgres() as c:
            c.execute(
                "INSERT INTO trades (timestamp,action,symbol,amount,qty,price,pnl,fee,fee_asset,reason,mode,session_id,session_name,binance_order_id)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (ts, action, symbol, amount, qty, price, pnl, fee, fee_asset, reason, mode, session_id, session_name, binance_order_id),
            )
    else:
        with _sqlite() as c:
            c.execute(
                "INSERT INTO trades (timestamp,action,symbol,amount,qty,price,pnl,fee,fee_asset,reason,mode,session_id,session_name,binance_order_id)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, action, symbol, amount, qty, price, pnl, fee, fee_asset, reason, mode, session_id, session_name, binance_order_id),
            )


def update_trade_binance_id(trade_pk, binance_order_id: str) -> None:
    """Backfill the Binance trade id onto an already-recorded trade row.

    Used by the Binance import to tag the agent's own historical trades (which
    predate id capture) so re-imports dedupe cleanly. No-op on Firestore, whose
    history rows don't expose a stable document id through ``load_history``.
    """
    if _USE_FIRESTORE:
        return
    elif _USE_POSTGRES:
        with _postgres() as c:
            c.execute("UPDATE trades SET binance_order_id=%s WHERE id=%s",
                      (binance_order_id, trade_pk))
    else:
        with _sqlite() as c:
            c.execute("UPDATE trades SET binance_order_id=? WHERE id=?",
                      (binance_order_id, trade_pk))


def list_simulation_sessions() -> list[dict]:
    """Return distinct simulation sessions ordered by most recent first."""
    if _USE_FIRESTORE:
        docs = [doc.to_dict() for doc in
                _fs().collection("trades").stream()]
        sim_docs = [d for d in docs if d.get("mode") == "simulation" and d.get("session_id")]
        groups: dict[str, dict] = {}
        for d in sim_docs:
            sid = d["session_id"]
            ts  = d.get("timestamp", "")
            if sid not in groups:
                groups[sid] = {
                    "session_id":   sid,
                    "session_name": d.get("session_name"),
                    "start_ts":     ts,
                    "end_ts":       ts,
                    "trade_count":  0,
                }
            g = groups[sid]
            if ts < g["start_ts"]:
                g["start_ts"] = ts
            if ts > g["end_ts"]:
                g["end_ts"] = ts
            g["trade_count"] += 1
        return sorted(groups.values(), key=lambda x: x["start_ts"], reverse=True)
    elif _USE_POSTGRES:
        with _postgres() as c:
            c.execute(
                "SELECT session_id, session_name, MIN(timestamp) as start_ts,"
                " MAX(timestamp) as end_ts, COUNT(*) as trade_count"
                " FROM trades WHERE mode='simulation' AND session_id IS NOT NULL"
                " GROUP BY session_id, session_name ORDER BY start_ts DESC"
            )
            return [dict(r) for r in c.fetchall()]
    else:
        with _sqlite() as c:
            rows = c.execute(
                "SELECT session_id, session_name, MIN(timestamp) as start_ts,"
                " MAX(timestamp) as end_ts, COUNT(*) as trade_count"
                " FROM trades WHERE mode='simulation' AND session_id IS NOT NULL"
                " GROUP BY session_id ORDER BY start_ts DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def sum_fees(mode: str | None = None) -> float:
    """Total fees across all trades, computed server-side.

    Avoids loading the whole trade history just to sum a column.
    """
    if _USE_FIRESTORE:
        q = _fs().collection("trades")
        if mode:
            q = q.where("mode", "==", mode)
        return float(sum(float(d.to_dict().get("fee") or 0) for d in q.stream()))
    if _USE_POSTGRES:
        with _postgres() as c:
            if mode:
                c.execute("SELECT COALESCE(SUM(fee),0) FROM trades WHERE mode=%s", (mode,))
            else:
                c.execute("SELECT COALESCE(SUM(fee),0) FROM trades")
            return float(c.fetchone()[0] or 0)
    with _sqlite() as c:
        if mode:
            row = c.execute(
                "SELECT COALESCE(SUM(fee),0) FROM trades WHERE mode=?", (mode,)
            ).fetchone()
        else:
            row = c.execute("SELECT COALESCE(SUM(fee),0) FROM trades").fetchone()
    return float(row[0] or 0)


def load_history(mode: str | None = None, limit: int = 500) -> list[dict]:
    if _USE_FIRESTORE:
        from google.cloud import firestore as _firestore  # type: ignore
        q = _fs().collection("trades").order_by(
            "timestamp", direction=_firestore.Query.DESCENDING
        ).limit(limit * 3 if mode else limit)
        docs = [doc.to_dict() for doc in q.stream()]
        if mode:
            docs = [d for d in docs if d.get("mode") == mode]
        return docs[:limit]
    elif _USE_POSTGRES:
        with _postgres() as c:
            if mode:
                c.execute(
                    "SELECT * FROM trades WHERE mode=%s ORDER BY timestamp DESC LIMIT %s",
                    (mode, limit),
                )
            else:
                c.execute(
                    "SELECT * FROM trades ORDER BY timestamp DESC LIMIT %s",
                    (limit,),
                )
            return [dict(r) for r in c.fetchall()]
    else:
        with _sqlite() as c:
            if mode:
                rows = c.execute(
                    "SELECT * FROM trades WHERE mode=? ORDER BY timestamp DESC LIMIT ?",
                    (mode, limit),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM trades ORDER BY timestamp DESC LIMIT ?",
                    (limit,),
                ).fetchall()
        return [dict(r) for r in rows]


def get_state(key: str) -> Any | None:
    if _USE_FIRESTORE:
        doc = _fs().collection("agent_state").document(key).get()
        return doc.to_dict() if doc.exists else None
    elif _USE_POSTGRES:
        with _postgres() as c:
            c.execute("SELECT value FROM agent_state WHERE key=%s", (key,))
            row = c.fetchone()
        return json.loads(row[0]) if row else None
    else:
        with _sqlite() as c:
            row = c.execute(
                "SELECT value FROM agent_state WHERE key=?", (key,)
            ).fetchone()
        return json.loads(row[0]) if row else None


def set_state(key: str, value: Any) -> None:
    ts = datetime.utcnow().isoformat()
    if _USE_FIRESTORE:
        _fs().collection("agent_state").document(key).set(value)
    elif _USE_POSTGRES:
        with _postgres() as c:
            c.execute(
                "INSERT INTO agent_state (key,value,updated_at) VALUES (%s,%s,%s)"
                " ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value, updated_at=EXCLUDED.updated_at",
                (key, json.dumps(value), ts),
            )
    else:
        with _sqlite() as c:
            c.execute(
                "INSERT INTO agent_state (key,value,updated_at) VALUES (?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
                (key, json.dumps(value), ts),
            )


def save_log(
    message: str,
    category: str = "technical",
    level: str = "info",
    mode: str = "real",
    cycle: int | None = None,
    session_id: str | None = None,
) -> None:
    ts = datetime.utcnow().isoformat()
    if _USE_FIRESTORE:
        _fs().collection("logs").add(dict(
            timestamp=ts, level=level, category=category,
            message=message, mode=mode, cycle=cycle, session_id=session_id,
        ))
    elif _USE_POSTGRES:
        with _postgres() as c:
            c.execute(
                "INSERT INTO logs (timestamp,level,category,message,mode,cycle,session_id)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (ts, level, category, message, mode, cycle, session_id),
            )
    else:
        with _sqlite() as c:
            c.execute(
                "INSERT INTO logs (timestamp,level,category,message,mode,cycle,session_id)"
                " VALUES (?,?,?,?,?,?,?)",
                (ts, level, category, message, mode, cycle, session_id),
            )


def load_logs(
    category: str | None = None,
    mode: str | None = None,
    session_id: str | None = None,
    limit: int = 200,
    since: str | None = None,
) -> list[dict]:
    """Load logs ordered by timestamp DESC.

    ``since`` (ISO timestamp) restricts to rows strictly newer than that point —
    used by the dashboard's incremental poll to avoid re-fetching the backlog
    every 8s (was the dominant Supabase egress source).
    """
    if _USE_FIRESTORE:
        from google.cloud import firestore as _firestore  # type: ignore
        q = _fs().collection("logs").order_by(
            "timestamp", direction=_firestore.Query.DESCENDING
        ).limit(limit * 4 if (category or mode or session_id) else limit)
        docs = [doc.to_dict() for doc in q.stream()]
        if category:
            docs = [d for d in docs if d.get("category") == category]
        if mode:
            docs = [d for d in docs if d.get("mode") == mode]
        if session_id:
            docs = [d for d in docs if d.get("session_id") == session_id]
        if since:
            docs = [d for d in docs if (d.get("timestamp") or "") > since]
        return docs[:limit]
    elif _USE_POSTGRES:
        conditions, params = [], []
        if category:
            conditions.append("category=%s")
            params.append(category)
        if mode:
            conditions.append("mode=%s")
            params.append(mode)
        if session_id:
            conditions.append("session_id=%s")
            params.append(session_id)
        if since:
            conditions.append("timestamp>%s")
            params.append(since)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        with _postgres() as c:
            c.execute(f"SELECT * FROM logs {where} ORDER BY timestamp DESC LIMIT %s", params)
            return [dict(r) for r in c.fetchall()]
    else:
        with _sqlite() as c:
            conditions, params = [], []
            if category:
                conditions.append("category=?")
                params.append(category)
            if mode:
                conditions.append("mode=?")
                params.append(mode)
            if session_id:
                conditions.append("session_id=?")
                params.append(session_id)
            if since:
                conditions.append("timestamp>?")
                params.append(since)
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            params.append(limit)
            rows = c.execute(
                f"SELECT * FROM logs {where} ORDER BY timestamp DESC LIMIT ?",
                params,
            ).fetchall()
        return [dict(r) for r in rows]


class DBLogHandler(logging.Handler):
    """Python logging handler that writes to the DB with auto-categorization."""

    _TRADE_KEYWORDS  = ("BUY", "SELL", "HOLD", "stop-loss", "trailing", "STOP-LOSS", "TRAILING",
                        "Acheté", "Vendu", "COOLDOWN")
    _MARKET_KEYWORDS = ("LLM", "Sentiment", "sentiment", "RSI", "score", "Score",
                        "BTCUSDC", "ETHUSDC", "SOLUSDC", "XRPUSDC", "BNBUSDC", "ADAUSDC", "AVAXUSDC",
                        "BTC", "ETH", "SOL", "XRP", "BNB", "ADA", "AVAX",
                        "analyse", "market", "Market", "marché",
                        "Fear", "Greed", "fear", "dominance", "dominance BTC",
                        "prix", "price", "Price", "Δmax", "Skip LLM",
                        "haussier", "baissier", "bullish", "bearish",
                        "signal", "Signal", "tendance", "Tendance",
                        "spread", "volume", "volatil")

    def __init__(self, mode: str = "real", session_id: str | None = None):
        super().__init__()
        self.mode = mode
        self._session_id = session_id
        self._cycle: int | None = None

    def set_cycle(self, cycle: int) -> None:
        self._cycle = cycle

    def _categorize(self, msg: str) -> str:
        if any(k in msg for k in self._TRADE_KEYWORDS):
            return "trade"
        if any(k in msg for k in self._MARKET_KEYWORDS):
            return "market"
        return "technical"

    def emit(self, record: logging.LogRecord) -> None:
        # Drop Werkzeug HTTP access logs (e.g. '127.0.0.1 - - "GET /api/..." 200 -')
        if record.name == "werkzeug":
            return
        try:
            msg   = self.format(record)
            cat   = self._categorize(msg)
            level = record.levelname.lower()
            save_log(msg, category=cat, level=level, mode=self.mode, cycle=self._cycle, session_id=self._session_id)
        except Exception:
            pass


def _allowed_emails() -> list[str]:
    return [e.strip().lower() for e in os.getenv("ALLOWED_EMAILS", "").split(",") if e.strip()]


def sync_users_from_env() -> None:
    """Sync the Firestore users collection with ALLOWED_EMAILS (add missing, remove extra)."""
    if not _USE_FIRESTORE:
        return
    allowed = set(_allowed_emails())
    fs = _fs()
    existing = {doc.id for doc in fs.collection("users").stream()}
    for email in allowed - existing:
        fs.collection("users").document(email).set({
            "email": email, "role": "admin",
            "added_at": datetime.utcnow().isoformat(),
        })
        logging.getLogger(__name__).info("Utilisateur ajouté : %s", email)
    for email in existing - allowed:
        fs.collection("users").document(email).delete()
        logging.getLogger(__name__).info("Utilisateur supprimé : %s", email)


def is_user_allowed(email: str) -> bool:
    """Check if a Google account email is authorised to access the dashboard."""
    if _USE_FIRESTORE:
        doc = _fs().collection("users").document(email).get()
        return doc.exists
    else:
        allowed = _allowed_emails()
        return not allowed or email in allowed   # empty list = allow all (local dev)


# ── Sessions ───────────────────────────────────────────────────────────────────

def upsert_session(
    session_id: str,
    name: str,
    mode: str = "simulation",
    initial_state: dict | None = None,
) -> None:
    """Create or update a session record."""
    ts = datetime.utcnow().isoformat()
    initial_state_json = json.dumps(initial_state) if initial_state else None
    if _USE_FIRESTORE:
        _fs().collection("sessions").document(session_id).set(
            {"id": session_id, "name": name, "mode": mode, "created_at": ts,
             "initial_state": initial_state_json},
            merge=True,
        )
    elif _USE_POSTGRES:
        with _postgres() as c:
            c.execute(
                "INSERT INTO sessions (id, name, mode, created_at, initial_state) VALUES (%s,%s,%s,%s,%s)"
                " ON CONFLICT(id) DO UPDATE SET name=EXCLUDED.name,"
                " initial_state=COALESCE(EXCLUDED.initial_state, sessions.initial_state)",
                (session_id, name, mode, ts, initial_state_json),
            )
    else:
        with _sqlite() as c:
            c.execute(
                "INSERT INTO sessions (id, name, mode, created_at, initial_state) VALUES (?,?,?,?,?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name,"
                " initial_state=COALESCE(excluded.initial_state, sessions.initial_state)",
                (session_id, name, mode, ts, initial_state_json),
            )


def rename_session(session_id: str, new_name: str) -> None:
    if _USE_FIRESTORE:
        _fs().collection("sessions").document(session_id).update({"name": new_name})
    elif _USE_POSTGRES:
        with _postgres() as c:
            c.execute("UPDATE sessions SET name=%s WHERE id=%s", (new_name, session_id))
    else:
        with _sqlite() as c:
            c.execute("UPDATE sessions SET name=? WHERE id=?", (new_name, session_id))


def find_session_by_name(name: str) -> list[dict]:
    """Return sessions whose name matches (case-insensitive, mode=simulation)."""
    if _USE_FIRESTORE:
        docs = _fs().collection("sessions").where("mode", "==", "simulation").stream()
        return [{"id": d.id, **d.to_dict()} for d in docs
                if d.to_dict().get("name", "").lower() == name.lower()]
    elif _USE_POSTGRES:
        with _postgres() as c:
            c.execute(
                "SELECT id, name, mode, created_at FROM sessions"
                " WHERE mode='simulation' AND LOWER(name)=LOWER(%s)",
                (name,),
            )
            return [dict(r) for r in c.fetchall()]
    else:
        with _sqlite() as c:
            rows = c.execute(
                "SELECT id, name, mode, created_at FROM sessions"
                " WHERE mode='simulation' AND LOWER(name)=LOWER(?)",
                (name,),
            ).fetchall()
        return [dict(r) for r in rows]


def delete_session(session_id: str) -> None:
    """Delete a session and all its associated trades, logs and analyses."""
    if _USE_FIRESTORE:
        _fs().collection("sessions").document(session_id).delete()
        for doc in _fs().collection("trades").where("session_id", "==", session_id).stream():
            doc.reference.delete()
        for doc in _fs().collection("logs").where("session_id", "==", session_id).stream():
            doc.reference.delete()
        for doc in _fs().collection("market_analyses").where("session_id", "==", session_id).stream():
            doc.reference.delete()
    elif _USE_POSTGRES:
        with _postgres() as c:
            c.execute("DELETE FROM sessions WHERE id=%s", (session_id,))
            c.execute("DELETE FROM trades WHERE session_id=%s", (session_id,))
            c.execute("DELETE FROM logs WHERE session_id=%s", (session_id,))
            c.execute("DELETE FROM market_analyses WHERE session_id=%s", (session_id,))
    else:
        with _sqlite() as c:
            c.execute("DELETE FROM sessions WHERE id=?", (session_id,))
            c.execute("DELETE FROM trades WHERE session_id=?", (session_id,))
            c.execute("DELETE FROM logs WHERE session_id=?", (session_id,))
            c.execute("DELETE FROM market_analyses WHERE session_id=?", (session_id,))


def list_simulation_sessions_v2() -> list[dict]:
    """Return sessions from the sessions table (authoritative), enriched with trade stats."""
    if _USE_FIRESTORE:
        docs = [{"id": doc.id, **doc.to_dict()} for doc in
                _fs().collection("sessions").where("mode", "==", "simulation").stream()]
        return sorted(docs, key=lambda x: x.get("created_at", ""), reverse=True)
    elif _USE_POSTGRES:
        with _postgres() as c:
            c.execute(
                "SELECT s.id, s.name, s.mode, s.created_at,"
                " COUNT(t.id) as trade_count,"
                " MIN(t.timestamp) as start_ts, MAX(t.timestamp) as end_ts,"
                " (SELECT MAX(cycle) FROM logs WHERE session_id = s.id) as cycle_count"
                " FROM sessions s"
                " LEFT JOIN trades t ON t.session_id = s.id AND t.mode = 'simulation'"
                " WHERE s.mode = 'simulation'"
                " GROUP BY s.id, s.name, s.mode, s.created_at ORDER BY s.created_at DESC"
            )
            return [dict(r) for r in c.fetchall()]
    else:
        with _sqlite() as c:
            rows = c.execute(
                "SELECT s.id, s.name, s.mode, s.created_at,"
                " COUNT(t.id) as trade_count,"
                " MIN(t.timestamp) as start_ts, MAX(t.timestamp) as end_ts,"
                " (SELECT MAX(cycle) FROM logs WHERE session_id = s.id) as cycle_count"
                " FROM sessions s"
                " LEFT JOIN trades t ON t.session_id = s.id AND t.mode = 'simulation'"
                " WHERE s.mode = 'simulation'"
                " GROUP BY s.id ORDER BY s.created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def list_real_sessions() -> list[dict]:
    """Return real-mode sessions enriched with trade stats.

    Mirrors ``list_simulation_sessions_v2`` but filters on ``mode='real'``.
    Trades pre-dating the session-per-real-run model (session_id NULL) are
    NOT included here — they remain visible under the global ``mode=real``
    history when no specific session is selected.
    """
    if _USE_FIRESTORE:
        docs = [{"id": doc.id, **doc.to_dict()} for doc in
                _fs().collection("sessions").where("mode", "==", "real").stream()]
        return sorted(docs, key=lambda x: x.get("created_at", ""), reverse=True)
    elif _USE_POSTGRES:
        with _postgres() as c:
            c.execute(
                "SELECT s.id, s.name, s.mode, s.created_at,"
                " COUNT(t.id) as trade_count,"
                " MIN(t.timestamp) as start_ts, MAX(t.timestamp) as end_ts,"
                " (SELECT MAX(cycle) FROM logs WHERE session_id = s.id) as cycle_count"
                " FROM sessions s"
                " LEFT JOIN trades t ON t.session_id = s.id AND t.mode = 'real'"
                " WHERE s.mode = 'real'"
                " GROUP BY s.id, s.name, s.mode, s.created_at ORDER BY s.created_at DESC"
            )
            return [dict(r) for r in c.fetchall()]
    else:
        with _sqlite() as c:
            rows = c.execute(
                "SELECT s.id, s.name, s.mode, s.created_at,"
                " COUNT(t.id) as trade_count,"
                " MIN(t.timestamp) as start_ts, MAX(t.timestamp) as end_ts,"
                " (SELECT MAX(cycle) FROM logs WHERE session_id = s.id) as cycle_count"
                " FROM sessions s"
                " LEFT JOIN trades t ON t.session_id = s.id AND t.mode = 'real'"
                " WHERE s.mode = 'real'"
                " GROUP BY s.id ORDER BY s.created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


def get_session(session_id: str) -> dict | None:
    """Return a single session record by id, or None if not found."""
    if _USE_FIRESTORE:
        doc = _fs().collection("sessions").document(session_id).get()
        if not doc.exists:
            return None
        return {"id": doc.id, **doc.to_dict()}
    elif _USE_POSTGRES:
        with _postgres() as c:
            c.execute("SELECT * FROM sessions WHERE id=%s", (session_id,))
            row = c.fetchone()
        return dict(row) if row else None
    else:
        with _sqlite() as c:
            row = c.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        return dict(row) if row else None


# ── Market analyses ────────────────────────────────────────────────────────────

def save_market_analysis(
    sentiment: str,
    summary: str,
    analyses: list,
    mode: str = "real",
    cycle: int | None = None,
    session_id: str | None = None,
    usage: dict | None = None,
    reasoning: list | None = None,
) -> None:
    ts = datetime.utcnow().isoformat()
    analyses_json  = json.dumps(analyses)
    usage_json     = json.dumps(usage) if usage else None
    reasoning_json = json.dumps(reasoning) if reasoning else None
    if _USE_FIRESTORE:
        _fs().collection("market_analyses").add(dict(
            timestamp=ts, cycle=cycle, mode=mode, session_id=session_id,
            sentiment=sentiment, summary=summary, analyses=analyses_json,
            usage=usage_json, reasoning=reasoning_json,
        ))
    elif _USE_POSTGRES:
        with _postgres() as c:
            c.execute(
                "INSERT INTO market_analyses (timestamp,cycle,mode,session_id,sentiment,summary,analyses,usage,reasoning)"
                " VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (ts, cycle, mode, session_id, sentiment, summary, analyses_json, usage_json, reasoning_json),
            )
    else:
        with _sqlite() as c:
            c.execute(
                "INSERT INTO market_analyses (timestamp,cycle,mode,session_id,sentiment,summary,analyses,usage,reasoning)"
                " VALUES (?,?,?,?,?,?,?,?,?)",
                (ts, cycle, mode, session_id, sentiment, summary, analyses_json, usage_json, reasoning_json),
            )


def load_market_analyses(
    mode: str | None = None,
    session_id: str | None = None,
    limit: int = 100,
) -> list[dict]:
    if _USE_FIRESTORE:
        from google.cloud import firestore as _firestore  # type: ignore
        q = _fs().collection("market_analyses").order_by(
            "timestamp", direction=_firestore.Query.DESCENDING
        ).limit(limit * 4 if (mode or session_id) else limit)
        docs = [doc.to_dict() for doc in q.stream()]
        if mode:
            docs = [d for d in docs if d.get("mode") == mode]
        if session_id:
            docs = [d for d in docs if d.get("session_id") == session_id]
        return docs[:limit]
    elif _USE_POSTGRES:
        conditions, params = [], []
        if mode:
            conditions.append("mode=%s")
            params.append(mode)
        if session_id:
            conditions.append("session_id=%s")
            params.append(session_id)
        where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
        params.append(limit)
        with _postgres() as c:
            c.execute(
                f"SELECT * FROM market_analyses {where} ORDER BY timestamp DESC LIMIT %s",
                params,
            )
            rows = c.fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["analyses"] = json.loads(d.get("analyses") or "[]")
            except Exception:
                d["analyses"] = []
            result.append(d)
        return result
    else:
        with _sqlite() as c:
            conditions, params = [], []
            if mode:
                conditions.append("mode=?")
                params.append(mode)
            if session_id:
                conditions.append("session_id=?")
                params.append(session_id)
            where = ("WHERE " + " AND ".join(conditions)) if conditions else ""
            params.append(limit)
            rows = c.execute(
                f"SELECT * FROM market_analyses {where} ORDER BY timestamp DESC LIMIT ?",
                params,
            ).fetchall()
        result = []
        for r in rows:
            d = dict(r)
            try:
                d["analyses"] = json.loads(d.get("analyses") or "[]")
            except Exception:
                d["analyses"] = []
            result.append(d)
        return result


# ── Cleanup ────────────────────────────────────────────────────────────────────

def clean_logs(
    older_than_days: int = 30,
    mode: str | None = None,
    session_id: str | None = None,
    keep_last: int | None = None,
    category: str | None = None,
) -> int:
    """Delete log entries. Returns the number of rows deleted.

    Options (mutually exclusive priority):
    - keep_last: keep only the N most recent log entries (ignores other filters)
    - older_than_days + mode + session_id + category: delete matching entries
      older than N days. Pass older_than_days=0 to ignore the age filter.
    """
    if _USE_FIRESTORE:
        return 0  # Not implemented for Firestore

    from datetime import timedelta

    ph = "%s" if _USE_POSTGRES else "?"  # placeholder style
    ctx = _postgres if _USE_POSTGRES else _sqlite

    with ctx() as c:
        if keep_last is not None:
            if _USE_POSTGRES:
                c.execute("SELECT COUNT(*) FROM logs")
                total = c.fetchone()[0]
            else:
                total = c.execute("SELECT COUNT(*) FROM logs").fetchone()[0]
            to_delete = max(0, total - keep_last)
            if to_delete == 0:
                return 0
            c.execute(
                f"DELETE FROM logs WHERE id IN "
                f"(SELECT id FROM logs ORDER BY timestamp ASC LIMIT {ph})",
                (to_delete,),
            )
            return to_delete

        conditions, params = [], []
        if older_than_days > 0:
            cutoff = (datetime.utcnow() - timedelta(days=older_than_days)).isoformat()
            conditions.append(f"timestamp < {ph}")
            params.append(cutoff)
        if mode:
            conditions.append(f"mode={ph}")
            params.append(mode)
        if session_id:
            conditions.append(f"session_id={ph}")
            params.append(session_id)
        if category:
            conditions.append(f"category={ph}")
            params.append(category)
        if not conditions:
            return 0  # refuse to delete everything by accident
        where = "WHERE " + " AND ".join(conditions)
        if _USE_POSTGRES:
            c.execute(f"SELECT COUNT(*) FROM logs {where}", params)
            count = c.fetchone()[0]
        else:
            count = c.execute(f"SELECT COUNT(*) FROM logs {where}", params).fetchone()[0]
        c.execute(f"DELETE FROM logs {where}", params)
        return count
