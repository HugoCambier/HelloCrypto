"""Initialize/migrate the DB schema.

Run this once after a schema change (new table, new column, new index, RLS
toggle) before deploying. The runtime no longer auto-migrates at module load,
because concurrent cold-starts on Vercel were deadlocking on the
``ALTER TABLE`` AccessExclusiveLock.

Usage:
    poetry run python -m scripts.init_db                 # uses local .env
    DATABASE_URL=postgres://... poetry run python -m scripts.init_db
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

load_dotenv()

from db.store import _USE_FIRESTORE, _USE_POSTGRES, init_db  # noqa: E402


def main() -> None:
    backend = "postgres" if _USE_POSTGRES else "firestore" if _USE_FIRESTORE else "sqlite"
    print(f"Initializing schema on {backend} backend…")
    init_db()
    print("Schema initialized.")


if __name__ == "__main__":
    main()
