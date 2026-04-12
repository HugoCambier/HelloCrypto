"""Data store — SQLite (local) or Firestore (GCP Cloud Run).

Auto-detected:
  - GOOGLE_CLOUD_PROJECT set (and no DATABASE_URL) → Firestore
  - Otherwise → SQLite (default path: data/hellocrypto.db)
"""
import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

_CLOUD_PROJECT = os.getenv("GOOGLE_CLOUD_PROJECT")
_USE_FIRESTORE = bool(_CLOUD_PROJECT) and not os.getenv("DATABASE_URL")


# ── SQLite ─────────────────────────────────────────────────────────────────────

def _db_path() -> Path:
    url = os.getenv("DATABASE_URL", "data/hellocrypto.db")
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
            session_name TEXT
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
            analyses   TEXT
        )""")


def _migrate_sqlite() -> None:
    """Add new columns to existing tables (migration for older DBs)."""
    with _sqlite() as c:
        for table, col, definition in [
            ("trades",   "session_id",    "TEXT"),
            ("trades",   "session_name",  "TEXT"),
            ("logs",     "session_id",    "TEXT"),
            ("sessions", "initial_state", "TEXT"),
        ]:
            try:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {definition}")
            except Exception:
                pass  # Column already exists


# ── Firestore ──────────────────────────────────────────────────────────────────

def _fs():
    from google.cloud import firestore  # type: ignore
    return firestore.Client()


# ── Public API ─────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create tables (SQLite) or no-op (Firestore)."""
    if not _USE_FIRESTORE:
        _init_sqlite()
        _migrate_sqlite()


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
) -> None:
    ts = datetime.utcnow().isoformat()
    if _USE_FIRESTORE:
        _fs().collection("trades").add(dict(
            timestamp=ts, action=action, symbol=symbol, amount=amount,
            qty=qty, price=price, pnl=pnl, fee=fee,
            fee_asset=fee_asset, reason=reason, mode=mode,
            session_id=session_id, session_name=session_name,
        ))
    else:
        with _sqlite() as c:
            c.execute(
                "INSERT INTO trades (timestamp,action,symbol,amount,qty,price,pnl,fee,fee_asset,reason,mode,session_id,session_name)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (ts, action, symbol, amount, qty, price, pnl, fee, fee_asset, reason, mode, session_id, session_name),
            )


def list_simulation_sessions() -> list[dict]:
    """Return distinct simulation sessions ordered by most recent first."""
    if _USE_FIRESTORE:
        from google.cloud import firestore as _firestore  # type: ignore
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
    else:
        with _sqlite() as c:
            rows = c.execute(
                "SELECT session_id, session_name, MIN(timestamp) as start_ts,"
                " MAX(timestamp) as end_ts, COUNT(*) as trade_count"
                " FROM trades WHERE mode='simulation' AND session_id IS NOT NULL"
                " GROUP BY session_id ORDER BY start_ts DESC"
            ).fetchall()
        return [dict(r) for r in rows]


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
) -> list[dict]:
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
        return docs[:limit]
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
    else:
        with _sqlite() as c:
            c.execute("UPDATE sessions SET name=? WHERE id=?", (new_name, session_id))


def find_session_by_name(name: str) -> list[dict]:
    """Return sessions whose name matches (case-insensitive, mode=simulation)."""
    if _USE_FIRESTORE:
        docs = _fs().collection("sessions").where("mode", "==", "simulation").stream()
        return [{"id": d.id, **d.to_dict()} for d in docs
                if d.to_dict().get("name", "").lower() == name.lower()]
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
    else:
        with _sqlite() as c:
            rows = c.execute(
                "SELECT s.id, s.name, s.mode, s.created_at,"
                " COUNT(t.id) as trade_count,"
                " MIN(t.timestamp) as start_ts, MAX(t.timestamp) as end_ts"
                " FROM sessions s"
                " LEFT JOIN trades t ON t.session_id = s.id AND t.mode = 'simulation'"
                " WHERE s.mode = 'simulation'"
                " GROUP BY s.id ORDER BY s.created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]


# ── Market analyses ────────────────────────────────────────────────────────────

def save_market_analysis(
    sentiment: str,
    summary: str,
    analyses: list,
    mode: str = "real",
    cycle: int | None = None,
    session_id: str | None = None,
) -> None:
    ts = datetime.utcnow().isoformat()
    analyses_json = json.dumps(analyses)
    if _USE_FIRESTORE:
        _fs().collection("market_analyses").add(dict(
            timestamp=ts, cycle=cycle, mode=mode, session_id=session_id,
            sentiment=sentiment, summary=summary, analyses=analyses_json,
        ))
    else:
        with _sqlite() as c:
            c.execute(
                "INSERT INTO market_analyses (timestamp,cycle,mode,session_id,sentiment,summary,analyses)"
                " VALUES (?,?,?,?,?,?,?)",
                (ts, cycle, mode, session_id, sentiment, summary, analyses_json),
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
) -> int:
    """Delete log entries. Returns the number of rows deleted.

    Options (mutually exclusive priority):
    - keep_last: keep only the N most recent log entries (ignores other filters)
    - older_than_days + mode + session_id: delete entries older than N days
    """
    if _USE_FIRESTORE:
        return 0  # Not implemented for Firestore

    with _sqlite() as c:
        if keep_last is not None:
            row = c.execute("SELECT COUNT(*) FROM logs").fetchone()
            total = row[0]
            to_delete = max(0, total - keep_last)
            if to_delete == 0:
                return 0
            c.execute(
                "DELETE FROM logs WHERE id IN "
                "(SELECT id FROM logs ORDER BY timestamp ASC LIMIT ?)",
                (to_delete,),
            )
            return to_delete

        conditions, params = [], []
        from datetime import timedelta
        cutoff = (datetime.utcnow() - timedelta(days=older_than_days)).isoformat()
        conditions.append("timestamp < ?")
        params.append(cutoff)
        if mode:
            conditions.append("mode=?")
            params.append(mode)
        if session_id:
            conditions.append("session_id=?")
            params.append(session_id)
        where = "WHERE " + " AND ".join(conditions)
        row = c.execute(f"SELECT COUNT(*) FROM logs {where}", params).fetchone()
        count = row[0]
        c.execute(f"DELETE FROM logs {where}", params)
        return count
