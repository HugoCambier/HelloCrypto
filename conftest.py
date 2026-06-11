"""Pytest session setup — never touch the production database.

The dev shell's ``DATABASE_URL`` points at the prod Postgres, and a few tests
exercise code paths that persist (agent cycles write trades + analyses). Point
the DB at a throwaway SQLite file for the whole test session so a run can never
mutate prod. Must run before ``db.store`` is first imported — pytest loads this
module before collecting tests, so setting the env here is early enough.
"""
import os
import tempfile

os.environ["DATABASE_URL"] = os.path.join(tempfile.gettempdir(), "hellocrypto_pytest.db")
# Guard against the Firestore branch being selected from a stray cloud env var.
os.environ.pop("GOOGLE_CLOUD_PROJECT", None)


def pytest_configure(config):
    """Create the SQLite schema so persisting code paths run against real tables."""
    from db.snapshots import init_snapshots
    from db.store import _init_sqlite
    _init_sqlite()
    init_snapshots()
