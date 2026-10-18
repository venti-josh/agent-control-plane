"""Retain unresolved local client command identities across CLI and MCP processes."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from .config import user_home


class PendingCommand:
    def __init__(self, base, method, path, data, key=None):
        if key is not None and (not key.strip() or len(key) > 200):
            raise ValueError('Request ID must contain 1 to 200 characters.')
        self.path = Path(os.getenv('ACP_COMMAND_JOURNAL', str(user_home() / 'client-commands.sqlite3')))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        os.close(fd)
        self.path.chmod(0o600)
        # Only hashes and opaque IDs are persisted; command bodies may contain private text.
        self.fingerprint = hashlib.sha256(json.dumps(
            [base.rstrip('/'), method, path, data], sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS pending (fingerprint TEXT PRIMARY KEY, request_id TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT request_id FROM pending WHERE fingerprint=?', (self.fingerprint,)).fetchone()
            if existing and key is not None and key != existing[0]:
                raise RuntimeError(f'Unresolved command {existing[0]} must be reconciled before using a different request ID.')
            self.key = existing[0] if existing else key or str(uuid4())
            db.execute('INSERT OR IGNORE INTO pending VALUES(?,?)', (self.fingerprint, self.key))

    def resolve(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            # An older concurrent response cannot delete a later command's identity.
            db.execute('DELETE FROM pending WHERE fingerprint=? AND request_id=?', (self.fingerprint, self.key))
