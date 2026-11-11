"""SQLite storage. One connection per process; the app runs in a single event loop."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, role TEXT NOT NULL,
  password_hash TEXT NOT NULL, country TEXT DEFAULT 'US', currency TEXT DEFAULT 'USD',
  prefs TEXT DEFAULT '{}', created_at REAL NOT NULL, session_epoch INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS billers (
  id TEXT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, country TEXT NOT NULL,
  city TEXT DEFAULT '', paypal_email TEXT NOT NULL, verified INTEGER DEFAULT 0,
  aliases TEXT DEFAULT '[]', user_id TEXT, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS bills (
  id TEXT PRIMARY KEY, poster_id TEXT NOT NULL, biller_id TEXT, biller_name TEXT NOT NULL,
  category TEXT NOT NULL, country TEXT NOT NULL, city TEXT DEFAULT '',
  currency TEXT NOT NULL, amount_minor INTEGER NOT NULL,
  settle_currency TEXT NOT NULL, settle_minor INTEGER NOT NULL, fx_note TEXT DEFAULT '',
  due_date TEXT NOT NULL, account_masked TEXT DEFAULT '', account_hash TEXT DEFAULT '',
  account_private TEXT DEFAULT '',
  status TEXT NOT NULL, risk_score INTEGER DEFAULT 0, risk_reasons TEXT DEFAULT '[]',
  decision TEXT DEFAULT '', extraction TEXT DEFAULT '{}', story TEXT DEFAULT '',
  story_consent INTEGER DEFAULT 0, poster_words TEXT DEFAULT '', alias TEXT DEFAULT '',
  doc_path TEXT DEFAULT '', doc_sha TEXT DEFAULT '', phash TEXT DEFAULT '', text_hash TEXT DEFAULT '',
  funded_minor INTEGER DEFAULT 0, payout_item_id TEXT DEFAULT '', payout_batch_id TEXT DEFAULT '',
  payout_txn_id TEXT DEFAULT '', thank_you TEXT DEFAULT '', reject_reason TEXT DEFAULT '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, listed_at REAL, paid_at REAL
);
CREATE TABLE IF NOT EXISTS contributions (
  id TEXT PRIMARY KEY, bill_id TEXT NOT NULL, sponsor_id TEXT NOT NULL,
  amount_minor INTEGER NOT NULL, currency TEXT NOT NULL, status TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'swipe', group_id TEXT DEFAULT '',
  order_id TEXT DEFAULT '', capture_id TEXT DEFAULT '', refunded_minor INTEGER DEFAULT 0,
  payer_email TEXT DEFAULT '', expires_at REAL, created_at REAL NOT NULL, captured_at REAL
);
CREATE INDEX IF NOT EXISTS ix_contrib_bill ON contributions(bill_id);
CREATE INDEX IF NOT EXISTS ix_contrib_order ON contributions(order_id);
CREATE TABLE IF NOT EXISTS orders (
  order_id TEXT PRIMARY KEY, sponsor_id TEXT NOT NULL, kind TEXT NOT NULL, currency TEXT NOT NULL,
  total_minor INTEGER NOT NULL, status TEXT NOT NULL, group_id TEXT DEFAULT '',
  created_at REAL NOT NULL, captured_at REAL
);
CREATE TABLE IF NOT EXISTS payout_items (
  sender_item_id TEXT PRIMARY KEY, batch_id TEXT NOT NULL, payout_item_id TEXT DEFAULT '',
  bill_id TEXT NOT NULL, amount_minor INTEGER NOT NULL, currency TEXT NOT NULL,
  receiver TEXT NOT NULL, status TEXT NOT NULL, transaction_id TEXT DEFAULT '',
  created_at REAL NOT NULL, updated_at REAL NOT NULL, sender_batch_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS payout_batches (
  batch_id TEXT PRIMARY KEY, sender_batch_id TEXT NOT NULL, status TEXT NOT NULL,
  created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL, kind TEXT NOT NULL,
  bill_id TEXT DEFAULT '', country TEXT DEFAULT '', city TEXT DEFAULT '', category TEXT DEFAULT '',
  currency TEXT DEFAULT '', amount_minor INTEGER DEFAULT 0, message TEXT NOT NULL,
  public INTEGER DEFAULT 1, audience TEXT DEFAULT '[]', ref TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, kind TEXT NOT NULL, subject TEXT NOT NULL,
  body TEXT NOT NULL, bill_id TEXT DEFAULT '', contribution_id TEXT DEFAULT '',
  created_at REAL NOT NULL, read INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS webhook_events (
  id TEXT PRIMARY KEY, event_type TEXT NOT NULL, received_at REAL NOT NULL,
  verified INTEGER NOT NULL, method TEXT DEFAULT '', processed INTEGER DEFAULT 0,
  note TEXT DEFAULT '', payload TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS passes (
  sponsor_id TEXT NOT NULL, bill_id TEXT NOT NULL, created_at REAL NOT NULL,
  PRIMARY KEY (sponsor_id, bill_id)
);
CREATE TABLE IF NOT EXISTS vault_tokens (
  id TEXT PRIMARY KEY, sponsor_id TEXT NOT NULL, setup_token_id TEXT DEFAULT '',
  payment_token_id TEXT DEFAULT '', payer_email TEXT DEFAULT '', status TEXT NOT NULL,
  created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY, sponsor_id TEXT NOT NULL, rules_text TEXT NOT NULL, policy TEXT NOT NULL,
  active INTEGER DEFAULT 1, created_at REAL NOT NULL, last_run REAL
);
CREATE TABLE IF NOT EXISTS agent_decisions (
  id TEXT PRIMARY KEY, agent_id TEXT NOT NULL, run_id TEXT NOT NULL, bill_id TEXT NOT NULL,
  decision TEXT NOT NULL, proposed_minor INTEGER DEFAULT 0, paid_minor INTEGER DEFAULT 0,
  currency TEXT DEFAULT '', reason TEXT DEFAULT '', rule TEXT DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS recurring (
  id TEXT PRIMARY KEY, sponsor_id TEXT NOT NULL, poster_id TEXT NOT NULL, category TEXT NOT NULL,
  biller_id TEXT DEFAULT '', max_minor INTEGER NOT NULL, currency TEXT NOT NULL,
  active INTEGER DEFAULT 1, created_at REAL NOT NULL, last_charged REAL
);
CREATE TABLE IF NOT EXISTS tickets (
  id TEXT PRIMARY KEY, user_id TEXT NOT NULL, subject TEXT NOT NULL, transcript TEXT NOT NULL,
  status TEXT NOT NULL, resolution TEXT DEFAULT '', created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
"""

# Indexes for the hot reads: public stats and decks by bill status, the scan limit by poster, sponsor pages by
# sponsor, payouts by bill. IF NOT EXISTS, so an existing database gains them on its next start.
INDEXES = """
CREATE INDEX IF NOT EXISTS ix_bills_status ON bills(status);
CREATE INDEX IF NOT EXISTS ix_bills_poster_created ON bills(poster_id, created_at);
CREATE INDEX IF NOT EXISTS ix_contrib_sponsor ON contributions(sponsor_id, status);
CREATE INDEX IF NOT EXISTS ix_payout_bill ON payout_items(bill_id);
CREATE INDEX IF NOT EXISTS ix_events_public ON events(public, id);
"""

# Columns added after the first release. connect() adds any that an existing database is missing.
MIGRATIONS = [
    ("payout_items", "sender_batch_id", "TEXT DEFAULT ''"),
    ("users", "session_epoch", "INTEGER NOT NULL DEFAULT 0"),
]

_conn: sqlite3.Connection | None = None
_lock = threading.RLock()


def connect(path: str | None = None) -> sqlite3.Connection:
    global _conn
    path = path or settings.db_path
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL" if path != ":memory:" else "PRAGMA journal_mode=MEMORY")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    if path != ":memory:":
        conn.execute("PRAGMA synchronous=NORMAL")  # safe with WAL: a power cut can lose the last commit, not corrupt
    for table, column, decl in MIGRATIONS:
        if column not in {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.executescript(INDEXES)
    _conn = conn
    return conn


def conn() -> sqlite3.Connection:
    if _conn is None:
        connect()
    assert _conn is not None
    return _conn


def q(sql: str, args: tuple | list = ()) -> list[dict]:
    with _lock:
        return [dict(r) for r in conn().execute(sql, args).fetchall()]


def one(sql: str, args: tuple | list = ()) -> dict | None:
    with _lock:
        r = conn().execute(sql, args).fetchone()
        return dict(r) if r else None


def run(sql: str, args: tuple | list = ()) -> int:
    with _lock:
        return conn().execute(sql, args).rowcount


@contextmanager
def tx():
    with _lock:
        c = conn()
        c.execute("BEGIN IMMEDIATE")
        try:
            yield c
        except BaseException:
            c.execute("ROLLBACK")
            raise
        else:
            c.execute("COMMIT")


def insert(table: str, row: dict) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    run(f"INSERT INTO {table} ({cols}) VALUES ({marks})", list(row.values()))


def update(table: str, key: str, key_value, **fields) -> None:
    sets = ", ".join(f"{k} = ?" for k in fields)
    run(f"UPDATE {table} SET {sets} WHERE {key} = ?", [*fields.values(), key_value])


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:10].upper()}"


def now() -> float:
    return time.time()


def jload(text: str | None, default=None):
    if not text:
        return default
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return default


def kv_get(k: str, default: str = "") -> str:
    r = one("SELECT v FROM kv WHERE k = ?", (k,))
    return r["v"] if r else default


def kv_set(k: str, v: str) -> None:
    run("INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v", (k, v))
