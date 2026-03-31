"""Data store — SQLite (local) or Firestore (GCP Cloud Run).

Auto-detected:
  - GOOGLE_CLOUD_PROJECT set (and no DATABASE_URL) → Firestore
  - Otherwise → SQLite (default path: data/hellocrypto.db)
"""
import json
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
            id        INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp TEXT    NOT NULL,
            action    TEXT    NOT NULL,
            symbol    TEXT,
            amount    REAL,
            qty       REAL,
            price     REAL,
            pnl       REAL,
            fee       REAL,
            fee_asset TEXT    DEFAULT 'USDC',
            reason    TEXT,
            mode      TEXT    DEFAULT 'real'
        )""")
        c.execute("""CREATE TABLE IF NOT EXISTS agent_state (
            key        TEXT PRIMARY KEY,
            value      TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")


# ── Firestore ──────────────────────────────────────────────────────────────────

def _fs():
    from google.cloud import firestore  # type: ignore
    return firestore.Client()


# ── Public API ─────────────────────────────────────────────────────────────────

def init_db() -> None:
    """Create tables (SQLite) or no-op (Firestore)."""
    if not _USE_FIRESTORE:
        _init_sqlite()


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
) -> None:
    ts = datetime.utcnow().isoformat()
    if _USE_FIRESTORE:
        _fs().collection("trades").add(dict(
            timestamp=ts, action=action, symbol=symbol, amount=amount,
            qty=qty, price=price, pnl=pnl, fee=fee,
            fee_asset=fee_asset, reason=reason, mode=mode,
        ))
    else:
        with _sqlite() as c:
            c.execute(
                "INSERT INTO trades (timestamp,action,symbol,amount,qty,price,pnl,fee,fee_asset,reason,mode)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (ts, action, symbol, amount, qty, price, pnl, fee, fee_asset, reason, mode),
            )


def load_history(mode: str | None = None, limit: int = 500) -> list[dict]:
    if _USE_FIRESTORE:
        from google.cloud import firestore as _firestore  # type: ignore
        q = _fs().collection("trades").order_by(
            "timestamp", direction=_firestore.Query.DESCENDING
        ).limit(limit)
        if mode:
            q = q.where(filter=_firestore.FieldFilter("mode", "==", mode))
        return [doc.to_dict() for doc in q.stream()]
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


def is_user_allowed(email: str) -> bool:
    """Check if a Google account email is authorised to access the dashboard."""
    if _USE_FIRESTORE:
        doc = _fs().collection("users").document(email).get()
        return doc.exists
    else:
        # Local dev: comma-separated list in ALLOWED_EMAILS env var
        allowed = [e.strip() for e in os.getenv("ALLOWED_EMAILS", "").split(",") if e.strip()]
        return not allowed or email in allowed   # empty list = allow all (local dev)


def list_users() -> list[dict]:
    if _USE_FIRESTORE:
        return [{"email": doc.id, **doc.to_dict()} for doc in _fs().collection("users").stream()]
    else:
        allowed = [e.strip() for e in os.getenv("ALLOWED_EMAILS", "").split(",") if e.strip()]
        return [{"email": e, "role": "admin"} for e in allowed]


def add_user(email: str, role: str = "viewer") -> None:
    if _USE_FIRESTORE:
        _fs().collection("users").document(email).set({
            "email": email, "role": role,
            "added_at": datetime.utcnow().isoformat(),
        })
    else:
        print(f"[LOCAL] Ajoute '{email}' à ALLOWED_EMAILS dans .env")


def remove_user(email: str) -> None:
    if _USE_FIRESTORE:
        _fs().collection("users").document(email).delete()
    else:
        print(f"[LOCAL] Supprime '{email}' de ALLOWED_EMAILS dans .env")
