"""Local, content-free Codex lifecycle observations. Never changes hook trust."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import difflib
import hashlib
import json
import os
import shlex
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

EVENTS = ('SessionStart', 'SessionEnd', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
          'PermissionRequest', 'SubagentStart', 'SubagentStop', 'Stop', 'Interrupt')
STALE_SECONDS = 300
# Codex runs no hook when a turn ends in an error (e.g. a usage limit), so long silence ends the turn.
EXPIRE_SECONDS = 1800
SCHEMA = '''
CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value REAL);
CREATE TABLE IF NOT EXISTS agents (
 id TEXT PRIMARY KEY, parent TEXT, kind TEXT, turn TEXT, owner TEXT,
 state TEXT, last_seen REAL);
CREATE TABLE IF NOT EXISTS turns (agent TEXT, turn TEXT, closed INTEGER,
 PRIMARY KEY(agent,turn));
CREATE TABLE IF NOT EXISTS intervals (agent TEXT, state TEXT, start REAL, end REAL);
CREATE INDEX IF NOT EXISTS intervals_time ON intervals(start,end);
CREATE TABLE IF NOT EXISTS waits (agent TEXT, token TEXT, tool TEXT,
 PRIMARY KEY(agent,token));
'''


def config_home():
    return Path(os.getenv('CODEX_HOME') or Path.home() / '.codex').expanduser()


def process_info(pid):
    try:
        result = subprocess.run(['ps', '-p', str(pid), '-o', 'ppid=', '-o', 'lstart=', '-o', 'comm='],
                                capture_output=True, text=True, timeout=1)
        fields = result.stdout.strip().split(None, 6)
        if result.returncode or len(fields) != 7:
            return None
        return int(fields[0]), ' '.join(fields[1:6]), fields[6]
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return None


def owner_identity():
    """Find the Codex process hosting this hook; do not read arguments or environment."""
    pid = os.getppid()
    for _ in range(12):
        info = process_info(pid)
        if not info:
            break
        parent, started, command = info
        if Path(command).name.lower() in {'codex', 'codex.exe'}:
            return f'{pid}:{started}'
        if parent <= 1 or parent == pid:
            break
        pid = parent
    return ''


def owner_alive(owner):
    if not owner:
        return None  # Process observation unavailable, not proof of idle.
    try:
        pid, started = owner.split(':', 1)
        info = process_info(int(pid))
    except ValueError:
        return None
    if info is None:
        # ps failures may be permission errors. kill(0) distinguishes absence.
        try:
            os.kill(int(pid), 0)
        except ProcessLookupError:
            return False
        except (PermissionError, ValueError):
            pass
        return None
    return info[1] == started


def identifier(value):
    return isinstance(value, str) and 0 < len(value) <= 256 and not any(ord(c) < 32 for c in value)


class CodexActivity:
    SOURCE, LABEL = 'codex_hooks', 'Codex'
    NOT_OBSERVED = 'Codex hooks need trust or a new turn'

    def __init__(self, path: Path):
        self.path = Path(path)

    def alive(self, owner):
        return owner_alive(owner)

    def idle_since(self, row):
        """When another trusted observation proved this agent idle; Codex has none."""
        return None

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        db = sqlite3.connect(self.path, timeout=1)
        os.chmod(self.path, 0o600)
        db.row_factory = sqlite3.Row
        db.executescript(SCHEMA)
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def transition(db, agent, state, now):
        old = db.execute('SELECT state FROM agents WHERE id=?', (agent,)).fetchone()
        if old and old['state'] == state:
            return
        db.execute('UPDATE intervals SET end=max(start,?) WHERE agent=? AND end IS NULL', (now, agent))
        db.execute('UPDATE agents SET state=? WHERE id=?', (state, agent))
        if state in {'running', 'waiting'}:
            db.execute('INSERT INTO intervals VALUES(?,?,?,NULL)', (agent, state, now))

    def close(self, db, agent, now, resumable=False):
        self.transition(db, agent, 'idle', now)
        if resumable:
            db.execute('UPDATE turns SET closed=2 WHERE agent=? AND closed=0', (agent,))
        else:
            db.execute('UPDATE turns SET closed=1 WHERE agent=?', (agent,))
        db.execute('DELETE FROM waits WHERE agent=?', (agent,))

    def record(self, event, owner):
        if not isinstance(event, dict):
            return
        name, session, turn = (event.get(k) for k in ('hook_event_name', 'session_id', 'turn_id'))
        if name not in EVENTS or not identifier(session):
            return
        if name not in {'SessionStart', 'SessionEnd'} and not identifier(turn):
            return
        child = name in {'SubagentStart', 'SubagentStop'}
        agent = event.get('agent_id') if child else session
        if not identifier(agent):
            return
        if child:
            turn = 'parent:' + turn
        now = time.time()
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute("INSERT OR IGNORE INTO metadata VALUES('since',?)", (now,))
            db.execute("INSERT OR REPLACE INTO metadata VALUES('last_event',?)", (now,))
            if name == 'SessionStart':
                return  # An open/idle chat is not a running agent.
            if name == 'SessionEnd':
                for row in db.execute('SELECT id FROM agents WHERE (id=? OR parent=?) AND owner=?', (session, session, owner)).fetchall():
                    self.close(db, row['id'], now)
                return
            old = db.execute('SELECT * FROM agents WHERE id=?', (agent,)).fetchone()
            seen = db.execute('SELECT closed FROM turns WHERE agent=? AND turn=?', (agent, turn)).fetchone()
            if name == 'SubagentStop' and seen and seen['closed'] in {0, 2}:
                self.close(db, agent, now, resumable=True)
                return
            if old and old['kind'] == 'child' and old['turn'].startswith('parent:') and not child and not seen:
                db.execute('UPDATE agents SET turn=? WHERE id=?', (turn, agent))
                old = db.execute('SELECT * FROM agents WHERE id=?', (agent,)).fetchone()
            # Stop is a continuation gate: another trusted hook can keep this turn alive.
            if seen and seen['closed'] == 2 and old and old['turn'] == turn and name == 'PreToolUse':
                db.execute('UPDATE turns SET closed=0 WHERE agent=? AND turn=?', (agent, turn))
                seen = db.execute('SELECT closed FROM turns WHERE agent=? AND turn=?', (agent, turn)).fetchone()
            if seen and (seen['closed'] or (old and old['turn'] != turn)):
                return
            parent = old['parent'] if old else (session if child else None)
            kind = old['kind'] if old else ('child' if child else 'chat')
            if old and old['turn'] != turn:
                if name not in {'UserPromptSubmit', 'SubagentStart'}:
                    return  # A late completion must never close a newer turn.
                self.close(db, agent, now)
                old = None
            if not old:
                if name in {'Stop', 'Interrupt', 'SubagentStop'}:
                    db.execute('INSERT OR REPLACE INTO turns VALUES(?,?,1)', (agent, turn))
                    return
                db.execute('INSERT OR REPLACE INTO agents VALUES(?,?,?,?,?,?,?)',
                           (agent, parent, kind, turn, owner, 'idle', now))
            elif now - old['last_seen'] > STALE_SECONDS or old['owner'] != owner:
                # Do not fill a gap in observation with invented running time.
                self.transition(db, agent, 'unknown', old['last_seen'])
                db.execute('DELETE FROM waits WHERE agent=?', (agent,))
            db.execute('INSERT OR IGNORE INTO turns VALUES(?,?,0)', (agent, turn))
            db.execute('UPDATE agents SET last_seen=?,owner=? WHERE id=?', (now, owner, agent))
            if name in {'Stop', 'Interrupt', 'SubagentStop'}:
                self.close(db, agent, now, resumable=name in {'Stop', 'SubagentStop'})
                return
            tool = event.get('tool_name', '')
            tool = tool if identifier(tool) else ''
            token = event.get('tool_use_id', '')
            token = token if identifier(token) else tool
            # Async questions do not block the agent and therefore are not a wait.
            blocking_input = tool.rsplit('__', 1)[-1].rsplit('.', 1)[-1] == 'request_user_input'
            if name == 'PermissionRequest' or (name == 'PreToolUse' and blocking_input):
                wait_id = 'permission:' + tool if name == 'PermissionRequest' else 'input:' + token
                db.execute('INSERT OR IGNORE INTO waits VALUES(?,?,?)', (agent, wait_id, tool))
            elif name == 'PostToolUse':
                db.execute('DELETE FROM waits WHERE agent=? AND token IN (?,?)',
                           (agent, 'permission:' + tool, 'input:' + token))
            waiting = db.execute('SELECT 1 FROM waits WHERE agent=?', (agent,)).fetchone()
            self.transition(db, agent, 'waiting' if waiting else 'running', now)
            # Rolling averages only require three hours of ended intervals.
            db.execute('DELETE FROM intervals WHERE end < ?', (now - 10800,))

    def summary(self):
        now = time.time()
        running = waiting = children = unknown = 0
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            owners = {}
            rows = db.execute("SELECT * FROM agents WHERE state IN ('running','waiting','unknown')").fetchall()
            for row in rows:
                owner = row['owner']
                if owner not in owners:
                    owners[owner] = self.alive(owner)
                idle = self.idle_since(row) if owners[owner] is not False else None
                if owners[owner] is False:
                    self.close(db, row['id'], row['last_seen'])
                elif idle is not None:
                    self.close(db, row['id'], idle, resumable=True)
                elif now - row['last_seen'] > EXPIRE_SECONDS:
                    # Resumable: a fresh tool start in the same turn is observed again.
                    self.close(db, row['id'], row['last_seen'], resumable=True)
                elif owners[owner] is None or now - row['last_seen'] > STALE_SECONDS:
                    self.transition(db, row['id'], 'unknown', row['last_seen'])
                    unknown += 1
                elif row['state'] == 'running':
                    running += 1
                    children += row['kind'] == 'child'
                elif row['state'] == 'waiting':
                    waiting += 1
                else:
                    unknown += 1
            averages = {}
            for label, seconds in [('30m',1800), ('1h',3600), ('3h',10800)]:
                total = db.execute('SELECT coalesce(sum(max(0,min(coalesce(end,?),?)-max(start,?))),0) '
                                   "FROM intervals WHERE state='running' AND start<? AND coalesce(end,?)>?",
                                   (now, now, now-seconds, now, now, now-seconds)).fetchone()[0]
                averages[label] = total / seconds
            approvals = db.execute("SELECT count(DISTINCT w.agent) FROM waits w JOIN agents a ON a.id=w.agent "
                                   "WHERE a.state='waiting' AND w.token LIKE 'permission:%'").fetchone()[0]
            since = db.execute("SELECT value FROM metadata WHERE key='since'").fetchone()
            last = db.execute("SELECT value FROM metadata WHERE key='last_event'").fetchone()
        available = since is not None and not unknown
        message = (self.NOT_OBSERVED if since is None else
                   f'{self.LABEL} activity uncertain: {unknown} silent agent{"s" * (unknown != 1)}' if unknown else
                   f'{self.LABEL} · approval status unconfirmed' if approvals else f'{self.LABEL} · observed activity')
        return dict(source=self.SOURCE, available=available, running=running, subagents=children,
                    waiting_on_input=waiting, approval_status_unconfirmed=approvals, unknown=unknown, averages=averages, message=message,
                    observed_since=since[0] if since else None, last_event_at=last[0] if last else None)


def atomic_json(path, data, mode=None):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, candidate = tempfile.mkstemp(dir=path.parent, prefix='.' + path.name)
    try:
        with os.fdopen(fd, 'w') as file:
            json.dump(data, file, indent=2); file.write('\n')
        if mode is not None:
            os.chmod(candidate, mode)
        os.replace(candidate, path)
    finally:
        Path(candidate).unlink(missing_ok=True)


def json_diff(path, original, data):
    before = original.decode().splitlines(keepends=True) if original is not None else []
    after = (json.dumps(data, indent=2) + '\n').splitlines(keepends=True)
    return ''.join(difflib.unified_diff(before, after, str(path), str(path) + ' (with ACP hooks)'))


def hook_manifest(database):
    return Path(database).with_suffix('.hooks.json')


def remove_entries(data, command):
    for event, groups in list(data.get('hooks', {}).items()):
        if not groups:
            continue
        kept = []
        for group in groups:
            hooks = [h for h in group.get('hooks', []) if h.get('command') != command]
            if hooks == group.get('hooks', []):
                kept.append(group)
            elif hooks:
                kept.append({**group, 'hooks': hooks})
        if kept:
            data['hooks'][event] = kept
        else:
            del data['hooks'][event]


def install_hooks(home, database, executable, dry_run=False):
    home, database = Path(home), Path(database).resolve()
    path = home / 'hooks.json'
    original = path.read_bytes() if path.exists() else None
    data = json.loads(original) if original is not None else {'hooks': {}}
    if not isinstance(data, dict) or not isinstance(data.setdefault('hooks', {}), dict):
        raise ValueError('Invalid Codex hooks.json; left unchanged')
    manifest = hook_manifest(database)
    previous = json.loads(manifest.read_text()) if manifest.exists() else {}
    if previous and previous['home'] != str(home.resolve()):
        raise ValueError('Uninstall the existing ACP hooks before changing Codex home')
    empty_events = previous.get('empty_events', []) + [k for k, v in data['hooks'].items() if not v]
    if previous:
        remove_entries(data, previous['command'])
    command = shlex.join([str(Path(executable).absolute()), '-I', '-m', 'acp.codex_activity',
                          '--database', str(database)])
    remove_entries(data, command)
    # Synchronous, fast, advisory hooks preserve ordering and never return decisions/context.
    for event in EVENTS:
        data['hooks'].setdefault(event, []).append({'hooks': [{'type':'command', 'command':command, 'timeout':2}]})
    if dry_run:
        return dict(hooks_file=str(path), dry_run=True, diff=json_diff(path, original, data))
    # Keep the hooks as they were before ACP; a refresh's input already carries ACP hooks.
    if original is not None and not previous:
        backup = path.with_name('hooks.acp-backup-' + hashlib.sha256(original).hexdigest()[:12] + '.json')
        if not backup.exists():
            backup.write_bytes(original); backup.chmod(0o600)
    if (path.read_bytes() if path.exists() else None) != original:
        raise ValueError('Codex hooks changed while installing; retry')
    changed = original is None or json.loads(original) != data
    if changed:
        atomic_json(path, data)
    atomic_json(manifest, dict(home=str(home.resolve()), command=command, empty_events=sorted(set(empty_events))))
    return dict(hooks_file=str(path), database=str(database), changed=changed, trust_required=True,
                next_step='Open /hooks in the Codex CLI, review and trust the ACP handlers. Start a new turn/session afterward.')


def uninstall_hooks(home, database):
    path, manifest = Path(home)/'hooks.json', hook_manifest(database)
    if not manifest.exists():
        return {'removed':False}
    saved = json.loads(manifest.read_text())
    if saved['home'] != str(Path(home).resolve()):
        raise ValueError('Codex home differs from the installed hooks')
    if path.exists():
        original = path.read_bytes()
        data = json.loads(original)
        remove_entries(data, saved['command'])
        for event in saved.get('empty_events', []):
            data['hooks'].setdefault(event, [])
        if path.read_bytes() != original:
            raise ValueError('Codex hooks changed while uninstalling; retry')
        atomic_json(path, data)
    manifest.unlink()
    return {'removed':True}


def main():
    parser = argparse.ArgumentParser(description='Advisory Codex activity hook; reads one event from stdin')
    parser.add_argument('--database', type=Path, required=True)
    args = parser.parse_args()
    try:
        # Bound parsing, discard everything except lifecycle metadata immediately.
        raw = sys.stdin.buffer.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            raise ValueError('Hook payload too large')
        CodexActivity(args.database).record(json.loads(raw), owner_identity())
    except Exception:
        # Never echo input or influence approval, tool execution, prompts, or continuation.
        print('ACP activity observation unavailable', file=sys.stderr)
    print('{}')


if __name__ == '__main__':
    main()
