"""Storage: Postgres when DATABASE_URL is set (production), else a local SQLite file.

The SQL is written once with %s placeholders and runs on both.
"""

from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path

URL = os.environ.get("DATABASE_URL", "")
PG = URL.startswith(("postgres://", "postgresql://"))

if PG:
    import psycopg
    from psycopg.rows import dict_row

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS leads (
        id TEXT PRIMARY KEY,
        data TEXT NOT NULL,
        first_seen TEXT NOT NULL,
        last_seen TEXT NOT NULL,
        web_checked TEXT
    )""",
    """CREATE TABLE IF NOT EXISTS pipeline (
        id TEXT PRIMARY KEY,
        stage TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        updated_by TEXT NOT NULL DEFAULT ''
    )""",
    """CREATE TABLE IF NOT EXISTS scans (
        id TEXT PRIMARY KEY,
        started_at TEXT NOT NULL,
        finished_at TEXT,
        started_by TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL,
        params TEXT NOT NULL,
        requests INTEGER NOT NULL DEFAULT 0,
        found INTEGER NOT NULL DEFAULT 0,
        added INTEGER NOT NULL DEFAULT 0,
        progress TEXT NOT NULL DEFAULT '',
        error TEXT
    )""",
]


@contextmanager
def conn():
    if PG:
        c = psycopg.connect(URL, row_factory=dict_row, autocommit=False)
    else:
        path = Path(os.environ.get("SQLITE_PATH", Path(__file__).parent / "vicino.db"))
        c = sqlite3.connect(path, timeout=30)
        c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    except BaseException:
        c.rollback()
        raise
    finally:
        c.close()


def _sql(q: str) -> str:
    return q if PG else q.replace("%s", "?")


def run(q: str, *args):
    with conn() as c:
        c.execute(_sql(q), args)


def many(q: str, rows):
    rows = list(rows)
    if not rows:
        return
    with conn() as c:
        if PG:
            with c.cursor() as cur:
                cur.executemany(q, rows)
        else:
            c.executemany(_sql(q), rows)


def select(q: str, *args) -> list[dict]:
    with conn() as c:
        return [dict(r) for r in c.execute(_sql(q), args).fetchall()]


def one(q: str, *args) -> dict | None:
    rows = select(q, *args)
    return rows[0] if rows else None


def init():
    with conn() as c:
        for s in SCHEMA:
            c.execute(s)


def dumps(o) -> str:
    return json.dumps(o, ensure_ascii=False, separators=(",", ":"))
