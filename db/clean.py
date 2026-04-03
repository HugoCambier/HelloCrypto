#!/usr/bin/env python3
"""Utility script to clean the HelloCrypto database.

Usage:
    python -m db.clean [options]

Examples:
    # Delete logs older than 30 days (default)
    python -m db.clean

    # Keep only the last 1000 log entries
    python -m db.clean --keep-last 1000

    # Delete simulation logs older than 7 days
    python -m db.clean --mode simulation --days 7

    # Delete ALL simulation logs
    python -m db.clean --mode simulation --days 0

    # Delete a specific simulation session (trades + logs + analyses)
    python -m db.clean --delete-session <session_id>

    # Show DB stats without deleting anything
    python -m db.clean --stats
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from db.store import _db_path, clean_logs, delete_session


def _db_stats() -> None:
    db = _db_path()
    if not db.exists():
        print(f"Base de données introuvable : {db}")
        return
    conn = sqlite3.connect(str(db))
    print(f"\n📊 Stats de la base : {db}")
    print(f"   Taille : {db.stat().st_size / 1024:.1f} KB\n")
    for table in ("trades", "logs", "sessions", "market_analyses", "agent_state"):
        try:
            count = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            print(f"   {table:<20} {count:>8} entrées")
        except Exception:
            print(f"   {table:<20}    (table absente)")
    # Oldest/newest log
    try:
        row = conn.execute(
            "SELECT MIN(timestamp), MAX(timestamp) FROM logs"
        ).fetchone()
        if row[0]:
            print(f"\n   Logs : {row[0][:19]}  →  {row[1][:19]}")
    except Exception:
        pass
    conn.close()
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Nettoyage de la base HelloCrypto")
    parser.add_argument("--stats",          action="store_true", help="Affiche les stats sans supprimer")
    parser.add_argument("--days",           type=int, default=30, help="Supprimer les logs plus vieux que N jours (défaut: 30)")
    parser.add_argument("--keep-last",      type=int, metavar="N", help="Garder seulement les N derniers logs")
    parser.add_argument("--mode",           choices=["real", "simulation"], help="Filtrer par mode")
    parser.add_argument("--session-id",     metavar="ID", help="Filtrer par session (logs) ou supprimer une session complète")
    parser.add_argument("--delete-session", metavar="ID", help="Supprimer une session complète (trades + logs + analyses)")
    args = parser.parse_args()

    if args.stats:
        _db_stats()
        return

    if args.delete_session:
        sid = args.delete_session
        confirm = input(f"Supprimer la session {sid} (trades + logs + analyses) ? [o/N] ").strip().lower()
        if confirm in ("o", "oui", "y", "yes"):
            delete_session(sid)
            print(f"✓ Session {sid} supprimée.")
        else:
            print("Annulé.")
        return

    _db_stats()

    deleted = clean_logs(
        older_than_days=args.days,
        mode=args.mode,
        session_id=args.session_id,
        keep_last=args.keep_last,
    )
    if args.keep_last is not None:
        print(f"✓ {deleted} entrées de logs supprimées (gardé les {args.keep_last} plus récentes).")
    else:
        print(f"✓ {deleted} entrées de logs supprimées (plus vieilles que {args.days} jours).")

    _db_stats()


if __name__ == "__main__":
    main()
