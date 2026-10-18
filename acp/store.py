from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL CHECK(state IN ('planning','todo','in_progress','waiting_on_input','ready_for_review','deploy','done')),
    provider TEXT NOT NULL CHECK(provider IN ('claude','codex','demo')),
    repo_path TEXT NOT NULL DEFAULT '',
    workspace TEXT,
    branch TEXT,
    session_id TEXT,
    blocker_comment_id TEXT,
    last_error TEXT,
    version INTEGER NOT NULL DEFAULT 1,
    linear_id TEXT UNIQUE,
    linear_url TEXT,
    linear_updated_at TEXT,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS comments (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    body TEXT NOT NULL,
    author TEXT NOT NULL,
    in_reply_to TEXT,
    external_id TEXT UNIQUE,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    deleted INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL,
    url TEXT NOT NULL,
    summary TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(task_id, url)
);
CREATE TABLE IF NOT EXISTS comment_files (
    comment_id TEXT NOT NULL REFERENCES comments(id),
    target TEXT NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT NOT NULL,
    content BLOB NOT NULL,
    digest TEXT NOT NULL,
    asset_url TEXT,
    PRIMARY KEY(comment_id, target)
);
CREATE TABLE IF NOT EXISTS runs (
    id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(id),
    status TEXT NOT NULL CHECK(status IN ('queued','running','succeeded','failed','cancelled','interrupted')),
    trigger TEXT NOT NULL,
    inputs TEXT NOT NULL,
    session_id TEXT,
    stop_requested INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_running_per_task ON runs(task_id) WHERE status='running';
CREATE UNIQUE INDEX IF NOT EXISTS one_queued_per_task ON runs(task_id) WHERE status='queued';
CREATE TABLE IF NOT EXISTS subagent_activity (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL REFERENCES runs(id),
    agent_id TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'running' CHECK(state IN ('running','waiting_on_input')),
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE UNIQUE INDEX IF NOT EXISTS one_active_subagent
ON subagent_activity(run_id,agent_id) WHERE finished_at IS NULL;
CREATE INDEX IF NOT EXISTS subagent_history ON subagent_activity(started_at,finished_at);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL,
    task_id TEXT,
    origin TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS task_events ON events(task_id,id);
CREATE TABLE IF NOT EXISTS subscriptions (
    id TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    types TEXT NOT NULL,
    secret TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS outbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL,
    dedupe_key TEXT NOT NULL UNIQUE,
    payload TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    available_at REAL NOT NULL,
    last_error TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS outbox_ready ON outbox(status,available_at,id);
CREATE TABLE IF NOT EXISTS command_receipts (
    key TEXT PRIMARY KEY,
    operation TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS receipts (
    source TEXT NOT NULL,
    id TEXT NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY(source,id)
);
CREATE TABLE IF NOT EXISTS desktop_runs (
    run_id TEXT PRIMARY KEY REFERENCES runs(id),
    phase TEXT NOT NULL CHECK(phase IN ('dispatched','claimed','attached','complete')),
    thread_id TEXT
);
CREATE TABLE IF NOT EXISTS desktop_inputs (
    comment_id TEXT PRIMARY KEY REFERENCES comments(id),
    run_id TEXT NOT NULL REFERENCES runs(id),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','claimed','delivered'))
);
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""


def encode(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def row(value: sqlite3.Row | None) -> dict | None:
    return dict(value) if value is not None else None


class Store:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript(SCHEMA)
            run_columns = {r['name'] for r in db.execute('PRAGMA table_info(runs)')}
            if 'activity_finished_at' not in run_columns:
                db.execute('ALTER TABLE runs ADD COLUMN activity_finished_at REAL')
            desktop_columns = {r['name'] for r in db.execute('PRAGMA table_info(desktop_runs)')}
            for name in ('dispatched_at', 'queue_accepted_at', 'claimed_at', 'delayed_at', 'recovery_at', 'ack_delayed_at'):
                if name not in desktop_columns:
                    db.execute(f'ALTER TABLE desktop_runs ADD COLUMN {name} REAL')
            if 'send_started_at' not in desktop_columns:
                db.execute('ALTER TABLE desktop_runs ADD COLUMN send_started_at REAL')
                # Old claimed handoffs may already have sent their prompt. Never grant another send.
                db.execute("UPDATE desktop_runs SET send_started_at=coalesce(claimed_at,0) WHERE phase='claimed'")
            for name, kind in [('delivery_attempts', 'INTEGER NOT NULL DEFAULT 0'),
                               ('queue_inflight', 'INTEGER NOT NULL DEFAULT 0'),
                               ('queue_error', 'TEXT'), ('next_attempt_at', 'REAL')]:
                if name not in desktop_columns:
                    db.execute(f'ALTER TABLE desktop_runs ADD COLUMN {name} {kind}')
            child_columns = {r['name'] for r in db.execute('PRAGMA table_info(subagent_activity)')}
            if 'state' not in child_columns:
                db.execute("ALTER TABLE subagent_activity ADD COLUMN state TEXT NOT NULL DEFAULT 'running'")
            columns = {r['name'] for r in db.execute('PRAGMA table_info(tasks)')}
            if 'hidden' not in columns:
                db.execute('ALTER TABLE tasks ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0')
            if 'linear_watch_since' not in columns:
                db.execute('ALTER TABLE tasks ADD COLUMN linear_watch_since REAL NOT NULL DEFAULT 0')

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=10000")
        try:
            yield db
        finally:
            db.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        # A task mutation and its events/outbox/jobs either all commit or none do.
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def all(self, sql: str, values: tuple = ()) -> list[dict]:
        with self.connect() as db:
            return [dict(r) for r in db.execute(sql, values).fetchall()]

    def one(self, sql: str, values: tuple = ()) -> dict | None:
        with self.connect() as db:
            return row(db.execute(sql, values).fetchone())

    def events(self, after: int = 0, task_id: str | None = None, limit: int = 200) -> list[dict]:
        where, args = "id > ?", [after]
        if task_id:
            where += " AND task_id = ?"
            args.append(task_id)
        args.append(min(limit, 1000))
        events = self.all(f"SELECT * FROM events WHERE {where} ORDER BY id LIMIT ?", tuple(args))
        for event in events:
            event["data"] = json.loads(event["data"])
            event["schema_version"] = 1
        return events

    @staticmethod
    def enqueue(db: sqlite3.Connection, kind: str, key: str, payload: dict) -> None:
        now = time.time()
        db.execute(
            "INSERT OR IGNORE INTO outbox(kind,dedupe_key,payload,available_at,created_at) VALUES(?,?,?,?,?)",
            (kind, key, encode(payload), now, now),
        )
