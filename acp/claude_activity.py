"""Local, content-free Claude Code lifecycle observations. Never changes permissions or trust.

The Claude Code CLI, the desktop app's Code tab and the IDE extensions all read user-level
hooks from one settings file, so a single set of handlers observes every local surface.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
import time
from pathlib import Path

from . import codex_activity
from .codex_activity import (STALE_SECONDS, CodexActivity, atomic_json, hook_manifest, identifier,
                             json_diff, owner_alive, process_info, remove_entries)

EVENTS = ('SessionStart', 'SessionEnd', 'UserPromptSubmit', 'PreToolUse', 'PostToolUse',
          'PostToolUseFailure', 'PermissionRequest', 'Notification', 'Stop', 'StopFailure',
          'SubagentStart', 'SubagentStop')
# Only the idle prompt says anything about activity; other notifications would each cost a process.
MATCHERS = {'Notification': 'idle_prompt'}
# Events that prove an agent is working now. Anything else can only continue observed work.
STARTS = {'UserPromptSubmit', 'PreToolUse', 'PermissionRequest', 'SubagentStart'}
# Every end is resumable: a Stop hook can continue a turn and a subagent can be resumed.
ENDS = {'Stop', 'StopFailure', 'SubagentStop', 'idle_prompt'}
QUESTION_TOOLS = {'AskUserQuestion'}
SHELLS = {'sh', 'bash', 'zsh', 'dash', 'fish', 'env'}
OWNER_NAMES = {'claude', 'claude.exe'}
TIMEOUT = 5
PAYLOAD_LIMIT = 8 * 1024 * 1024  # Tool results ride along on PostToolUse; they are parsed, never kept.


def config_home():
    return Path(os.getenv('CLAUDE_CONFIG_DIR') or Path.home() / '.claude').expanduser()


def owner_identity():
    """Find the Claude Code process hosting this hook; do not read arguments or environment."""
    pid, fallback = os.getppid(), ''
    for _ in range(12):
        info = process_info(pid)
        if not info:
            break
        parent, started, command = info
        name = Path(command).name.lstrip('-')
        # Exact case: the desktop app's own "Claude" process outlives every session it hosts.
        if name in OWNER_NAMES:
            return f'{pid}:{started}'
        if not fallback and name.lower() not in SHELLS:
            fallback = f'{pid}:{started}'  # The hook's host, e.g. a Node-launched Claude Code.
        if parent <= 1 or parent == pid:
            break
        pid = parent
    return fallback


def registry_idle_since(home, owner, session):
    """When Claude Code's own session registry last marked this session idle, if it says so.

    Interrupts and rejected approvals end a turn without any hook. The registry is
    undocumented, so only an explicit idle status for the same process and session counts;
    anything missing or unreadable proves nothing. Only these three fields are used.
    """
    try:
        pid = int(owner.split(':', 1)[0])
        data = json.loads((Path(home) / 'sessions' / f'{pid}.json').read_bytes()[:65536])
        updated = data.get('statusUpdatedAt')
        if (data.get('pid') != pid or data.get('sessionId') != session or data.get('status') != 'idle'
                or isinstance(updated, bool) or not isinstance(updated, (int, float))):
            return None
        return updated / 1000
    except (OSError, ValueError, AttributeError):
        return None


class ClaudeActivity(CodexActivity):
    SOURCE, LABEL = 'claude_hooks', 'Claude'
    NOT_OBSERVED = 'Claude hooks not installed or no new session yet'

    def __init__(self, path: Path, home: Path | None = None):
        super().__init__(path)
        self.home = Path(home) if home else None

    def alive(self, owner):
        return owner_alive(owner)

    def idle_since(self, row):
        if row['kind'] != 'chat':
            return None
        if self.home is None:
            manifest = hook_manifest(self.path)
            try:
                self.home = Path(json.loads(manifest.read_text())['home'])
            except (OSError, ValueError, KeyError, TypeError):
                self.home = config_home()
        idle = registry_idle_since(self.home, row['owner'], row['id'])
        return idle if idle is not None and idle >= row['last_seen'] else None

    def record(self, event, owner):
        if not isinstance(event, dict):
            return
        name, session = event.get('hook_event_name'), event.get('session_id')
        if name not in EVENTS or not identifier(session):
            return
        if name == 'Notification':
            if event.get('notification_type') != 'idle_prompt':
                return
            name = 'idle_prompt'
        child = event.get('agent_id')
        if child is not None and not identifier(child):
            return
        # Internal helpers (prompt suggestions, /btw) report subagents without a type.
        if name in {'SubagentStart', 'SubagentStop'} and not (child and identifier(event.get('agent_type'))):
            return
        agent = child or session
        # Claude has no turn ID; prompt_id names the prompt every later event belongs to.
        turn = event.get('prompt_id') if identifier(event.get('prompt_id')) else ''
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
            if old and not child and turn and old['turn'] and old['turn'] != turn:
                if name not in STARTS:
                    return  # A late event from an earlier prompt must never touch a newer one.
                self.close(db, agent, now)
                old = None
            elif old and old['state'] == 'idle':
                # Finished work is revived only by fresh work, never by a replayed prompt.
                if name not in STARTS or (name == 'UserPromptSubmit' and turn and old['turn'] == turn):
                    return
            if not old:
                if name not in STARTS:
                    return  # Work whose start was never observed is not counted.
                db.execute('INSERT OR REPLACE INTO agents VALUES(?,?,?,?,?,?,?)',
                           (agent, session if child else None, 'child' if child else 'chat', turn, owner, 'idle', now))
            elif old['state'] != 'idle' and (now - old['last_seen'] > STALE_SECONDS or old['owner'] != owner):
                # Do not fill a gap in observation with invented running time.
                self.transition(db, agent, 'unknown', old['last_seen'])
                db.execute('DELETE FROM waits WHERE agent=?', (agent,))
            db.execute('UPDATE agents SET last_seen=?,owner=? WHERE id=?', (now, owner, agent))
            if turn and not child and name in STARTS:
                db.execute('UPDATE agents SET turn=? WHERE id=?', (turn, agent))
            if name in ENDS:
                self.close(db, agent, now, resumable=True)
                if name == 'StopFailure':
                    # An API error such as a usage limit stops the session's subagents too.
                    for row in db.execute("SELECT id FROM agents WHERE parent=? AND state<>'idle'", (session,)).fetchall():
                        self.close(db, row['id'], now, resumable=True)
                return
            tool = event.get('tool_name', '')
            tool = tool if identifier(tool) else ''
            token = event.get('tool_use_id', '')
            token = token if identifier(token) else tool
            if name == 'PermissionRequest' or (name == 'PreToolUse' and tool in QUESTION_TOOLS):
                wait_id = 'permission:' + tool if name == 'PermissionRequest' else 'input:' + token
                db.execute('INSERT OR IGNORE INTO waits VALUES(?,?,?)', (agent, wait_id, tool))
            elif name in {'PostToolUse', 'PostToolUseFailure'}:
                db.execute('DELETE FROM waits WHERE agent=? AND token IN (?,?)',
                           (agent, 'permission:' + tool, 'input:' + token))
            elif name == 'UserPromptSubmit':
                db.execute('DELETE FROM waits WHERE agent=?', (agent,))
            waiting = db.execute('SELECT 1 FROM waits WHERE agent=?', (agent,)).fetchone()
            self.transition(db, agent, 'waiting' if waiting else 'running', now)
            # Rolling averages only require three hours of ended intervals.
            db.execute('DELETE FROM intervals WHERE end < ?', (now - 10800,))


def merge(*sources):
    """Combine per-source summaries; one missing or stale source never blanks the others."""
    observed = [s for s in sources if s.get('observed_since') is not None]
    trusted = [s for s in observed if s.get('available')]
    label = {'codex_hooks': 'Codex', 'claude_hooks': 'Claude'}

    def total(key):
        return sum(s.get(key, 0) for s in observed)
    uncertain = [f'{label.get(s["source"], s["source"])} uncertain: {s["unknown"]} silent'
                 if s.get('unknown') else s['message'] for s in observed if not s.get('available')]
    failed = [s['message'] for s in sources if s.get('error')]
    if len(observed) == 1:
        message = ' · '.join([observed[0]['message']] + failed)
    elif not observed:
        message = ' · '.join(failed) or 'No Codex or Claude hook activity observed yet'
    else:
        message = ' · '.join(uncertain + failed +
                             (['approval unconfirmed'] if total('approval_status_unconfirmed') else []))
    return dict(
        source='hooks', available=bool(trusted), running=total('running'), subagents=total('subagents'),
        waiting_on_input=total('waiting_on_input'), approval_status_unconfirmed=total('approval_status_unconfirmed'),
        unknown=total('unknown'), averages={k: sum(s['averages'][k] for s in observed) for k in ('30m', '1h', '3h')},
        message=message, observed_since=min((s['observed_since'] for s in observed), default=None),
        last_event_at=max((s['last_event_at'] for s in observed if s.get('last_event_at') is not None), default=None),
        sources={label.get(s['source'], s['source']).lower(): s for s in sources})


def unavailable(source, label):
    return dict(source=source, available=False, running=0, subagents=0, waiting_on_input=0,
                approval_status_unconfirmed=0, unknown=0, averages={'30m': 0.0, '1h': 0.0, '3h': 0.0},
                message=f'{label} activity unavailable', observed_since=None, last_event_at=None, error=True)


def combined_summary(data_dir):
    summaries = []
    for tracker in (CodexActivity(Path(data_dir) / 'codex-activity.sqlite3'),
                    ClaudeActivity(Path(data_dir) / 'claude-activity.sqlite3')):
        try:
            summaries.append(tracker.summary())
        except Exception:
            # A locked or damaged database is that source's problem alone.
            summaries.append(unavailable(tracker.SOURCE, tracker.LABEL))
    return merge(*summaries)


def install_hooks(home, database, executable, dry_run=False):
    home, database = Path(home), Path(database).resolve()
    # Write through a symlinked settings file (e.g. a dotfiles checkout) instead of replacing it.
    path = (home / 'settings.json').resolve()
    original = path.read_bytes() if path.exists() else None
    data = json.loads(original) if original is not None else {}
    if not isinstance(data, dict):
        raise ValueError('Invalid Claude settings.json; left unchanged')
    had_hooks = 'hooks' in data
    if not isinstance(data.setdefault('hooks', {}), dict):
        raise ValueError('Invalid Claude settings.json hooks; left unchanged')
    manifest = hook_manifest(database)
    previous = json.loads(manifest.read_text()) if manifest.exists() else {}
    if previous and previous['home'] != str(home.resolve()):
        raise ValueError('Uninstall the existing ACP Claude hooks before changing the Claude config directory')
    empty_events = previous.get('empty_events', []) + [k for k, v in data['hooks'].items() if not v]
    added_key = previous.get('added_hooks_key', not had_hooks)
    if previous:
        remove_entries(data, previous['command'])
    command = shlex.join([str(Path(executable).absolute()), '-I', '-m', 'acp.claude_activity',
                          '--database', str(database)])
    remove_entries(data, command)
    # Synchronous, fast, advisory hooks preserve ordering and never return decisions or context.
    for event in EVENTS:
        group = {'hooks': [{'type': 'command', 'command': command, 'timeout': TIMEOUT}]}
        data['hooks'].setdefault(event, []).append({'matcher': MATCHERS[event], **group} if event in MATCHERS else group)
    if dry_run:
        return dict(settings_file=str(path), dry_run=True, diff=json_diff(path, original, data))
    mode = path.stat().st_mode & 0o777 if original is not None else None
    # Keep the settings as they were before ACP; a reinstall's input already carries ACP hooks.
    if original is not None and not previous:
        backup = path.with_name('settings.acp-backup-' + hashlib.sha256(original).hexdigest()[:12] + '.json')
        if not backup.exists():
            backup.write_bytes(original); backup.chmod(0o600)
    if (path.read_bytes() if path.exists() else None) != original:
        raise ValueError('Claude settings changed while installing; retry')
    changed = original is None or json.loads(original) != data
    if changed:
        atomic_json(path, data, mode)
    atomic_json(manifest, dict(home=str(home.resolve()), settings_file=str(path), command=command,
                               empty_events=sorted(set(empty_events)), added_hooks_key=added_key))
    return dict(settings_file=str(path), database=str(database), changed=changed,
                next_step='Start a new Claude Code session (CLI, desktop Code tab, or IDE); '
                          'sessions already running may keep the hooks they started with.')


def uninstall_hooks(home, database):
    manifest = hook_manifest(database)
    if not manifest.exists():
        return {'removed': False}
    saved = json.loads(manifest.read_text())
    if saved['home'] != str(Path(home).resolve()):
        raise ValueError('Claude config directory differs from the installed hooks')
    path = Path(saved.get('settings_file') or Path(home) / 'settings.json')
    if path.exists():
        original = path.read_bytes()
        data = json.loads(original)
        hooks = data.get('hooks')
        if isinstance(hooks, dict):
            remove_entries(data, saved['command'])
            for event in saved.get('empty_events', []):
                hooks.setdefault(event, [])
            if saved.get('added_hooks_key') and not hooks:
                del data['hooks']
        if path.read_bytes() != original:
            raise ValueError('Claude settings changed while uninstalling; retry')
        atomic_json(path, data, path.stat().st_mode & 0o777)
    manifest.unlink()
    return {'removed': True}


def setup_hooks(data_dir, executable, found, enabled=True):
    """Install or refresh every agent's activity hooks for `acp setup`.

    An agent gets hooks when its CLI was found, or when a manifest records an earlier install:
    that one is refreshed at its recorded home so a release's new events reach it. Each agent
    is reported on its own; a failure installs nothing for that agent and nothing here raises.
    """
    report = {}
    for name, module in (('codex', codex_activity), ('claude', sys.modules[__name__])):
        if not enabled:
            report[name] = {'status': 'skipped: ACP_ACTIVITY_HOOKS=0'}
            continue
        database = Path(data_dir) / f'{name}-activity.sqlite3'
        manifest = hook_manifest(database)
        try:
            previous = json.loads(manifest.read_text()) if manifest.exists() else {}
        except (OSError, ValueError):
            previous = None
        if not isinstance(previous, dict):
            report[name] = {'status': f'failed: unreadable manifest {manifest}'}
            continue
        if not found.get(name) and not previous:
            report[name] = {'status': 'skipped: not found'}
            continue
        home = Path(previous['home']) if isinstance(previous.get('home'), str) else module.config_home()
        try:
            result = module.install_hooks(home, database, executable)
        except Exception as exc:  # One agent's broken file must not stop setup.
            report[name] = {'status': f'failed: {exc}', 'home': str(home)}
            continue
        status = 'installed' if not previous else 'updated' if result['changed'] else 'already installed'
        entry = {'status': status, 'home': str(home), 'file': result.get('hooks_file') or result['settings_file']}
        if status != 'already installed':
            entry['next_step'] = result['next_step']
            if result.get('trust_required'):
                entry['trust_required'] = True  # Codex trusts hook commands only through its /hooks UI.
        current = module.config_home()
        if found.get(name) and home.resolve() != current.resolve():
            option = '--codex-home' if name == 'codex' else '--claude-config-dir'
            entry['note'] = (f'kept at the recorded home; the current home is {current}. To move them, run '
                             f'acp activity uninstall --target {name} {option} {shlex.quote(str(home))}, '
                             f'then acp activity install --target {name}.')
        report[name] = entry
    return report


def main():
    parser = argparse.ArgumentParser(description='Advisory Claude Code activity hook; reads one event from stdin')
    parser.add_argument('--database', type=Path, required=True)
    args = parser.parse_args()
    try:
        # Bound parsing, discard everything except lifecycle metadata immediately.
        raw = sys.stdin.buffer.read(PAYLOAD_LIMIT + 1)
        if len(raw) > PAYLOAD_LIMIT:
            raise ValueError('Hook payload too large')
        ClaudeActivity(args.database).record(json.loads(raw), owner_identity())
    except Exception:
        # Never echo input or influence approval, tool execution, prompts, or continuation.
        print('ACP activity observation unavailable', file=sys.stderr)
    # No stdout at all: on SessionStart and UserPromptSubmit it would become model context.


if __name__ == '__main__':
    main()
