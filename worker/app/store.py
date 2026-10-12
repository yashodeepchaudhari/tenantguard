"""Tenant repository: one SQLite file per tenant ({data_dir}/{tenant}.db).

Every public method takes the tenant from `tenant_var` only; there is no tenant
parameter, so a handler cannot reach another tenant's file by construction.
"""
from __future__ import annotations

import hashlib
import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Dict, FrozenSet, Iterator, List, Optional, Tuple

from .meter import Meters
from .tenancy import TENANT_RE, current_tenant

class UnknownTenantError(LookupError):
    """Tenant id is not in the configured allow-list."""


class StoreClosedError(RuntimeError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS items (key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS terms (term TEXT NOT NULL, key TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS terms_term ON terms(term);
"""


def seed_value(tenant: str, key: str, size: int = 512) -> str:
    """Deterministic row value; starts with the owner so leaks are detectable."""
    h = hashlib.sha256(f"{tenant}/{key}".encode()).hexdigest()
    body = (h * (size // len(h) + 1))[:size]
    return f"{tenant}|{key}|{body}"


class _Db:
    def __init__(self, path: Path) -> None:
        self.conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.executescript(_SCHEMA)
        self.lock = threading.Lock()


class TenantStore:
    def __init__(self, data_dir: Path, meters: Meters, seed_rows: int = 1000,
                 allowed: Optional[FrozenSet[str]] = None) -> None:
        self.dir = Path(data_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.meters = meters
        self.seed_rows = seed_rows
        self.allowed = allowed  # None = any well-formed id (tests); the app passes the configured tenants
        self._dbs: Dict[str, _Db] = {}
        self._lock = threading.Lock()
        self._closed = False

    # ---- lifecycle ----
    def path_for(self, tenant: str) -> Path:
        if tenant != "_canary":
            if not TENANT_RE.match(tenant):
                raise ValueError(f"bad tenant id {tenant!r}")
            if self.allowed is not None and tenant not in self.allowed:
                raise UnknownTenantError(f"tenant {tenant!r} is not configured")
        return self.dir / f"{tenant}.db"

    def reset_and_seed(self, tenants: List[str]) -> None:
        """Start from fresh files so every process start is deterministic."""
        self.close()
        self._closed = False
        for t in tenants + ["_canary"]:
            for suffix in ("", "-wal", "-shm"):
                p = Path(str(self.path_for(t)) + suffix)
                if p.exists():
                    p.unlink()
            self._db(t)  # creates the file and seeds it

    def _seed(self, tenant: str, db: _Db) -> None:
        now = time.time()
        rows = [(f"k{i:04d}", seed_value(tenant, f"k{i:04d}"), now) for i in range(self.seed_rows)]
        with db.lock:
            db.conn.execute("BEGIN")
            db.conn.executemany("INSERT OR REPLACE INTO items VALUES (?,?,?)", rows)
            db.conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            for db in self._dbs.values():
                with db.lock:  # never close a connection another thread is using
                    db.conn.close()
            self._dbs.clear()

    def _db(self, tenant: str) -> _Db:
        with self._lock:
            if self._closed:
                raise StoreClosedError("store is closed")
            db = self._dbs.get(tenant)
            if db is None:
                db = _Db(self.path_for(tenant))
                empty = db.conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
                if empty and tenant != "_canary" and self.seed_rows:
                    self._seed(tenant, db)  # a tenant added at runtime gets the same seed
                self._dbs[tenant] = db
        return db

    @contextmanager
    def _tx(self, tenant: Optional[str] = None) -> Iterator[sqlite3.Connection]:
        tenant = tenant or current_tenant()
        db = self._db(tenant)
        t0 = time.perf_counter()
        try:
            with db.lock:
                yield db.conn
        finally:
            if tenant != "_canary":
                self.meters.add_db(tenant, (time.perf_counter() - t0) * 1000)

    # ---- repository API (tenant from context only) ----
    def get(self, key: str) -> Optional[str]:
        with self._tx() as c:
            row = c.execute("SELECT value FROM items WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def put(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute("INSERT OR REPLACE INTO items VALUES (?,?,?)", (key, value, time.time()))

    def add_terms(self, key: str, terms: List[str]) -> None:
        with self._tx() as c:
            c.execute("BEGIN")
            c.execute("DELETE FROM terms WHERE key=?", (key,))
            c.executemany("INSERT INTO terms VALUES (?,?)", [(t, key) for t in terms])
            c.execute("COMMIT")

    def find_term(self, term: str) -> List[str]:
        with self._tx() as c:
            return [r[0] for r in c.execute("SELECT key FROM terms WHERE term=? LIMIT 100", (term,))]

    def page(self, after: str, limit: int) -> List[Tuple[str, str]]:
        with self._tx() as c:
            return c.execute("SELECT key, value FROM items WHERE key>? ORDER BY key LIMIT ?",
                             (after, limit)).fetchall()

    def count(self) -> int:
        with self._tx() as c:
            return c.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    # ---- canary (unattributed probe on its own file) ----
    def canary_probe(self) -> int:
        with self._tx("_canary") as c:
            c.execute("INSERT OR REPLACE INTO items VALUES ('probe', ?, ?)", (str(time.time()), time.time()))
            return c.execute("SELECT COUNT(*) FROM items").fetchone()[0]
