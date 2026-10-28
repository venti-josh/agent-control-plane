"""Read and plan migrations of Claude Desktop's local Code session records.

The unversioned record format is intentionally treated as opaque outside the
fields validated here. This module never reads credentials or Chromium stores.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from pathlib import Path

TOKEN = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]*\Z')
UI_CHECKS = [
    'Sign in to the destination account in Claude Desktop; CLI login is separate.',
    'Restart Claude and check session visibility, worktrees, subthreads and statuses; then reopen again.',
    'Reconnect account-bound connectors. Tool IDs in records do not prove access.',
    'Sidebar groups, pins, environments, Chromium stores and Cowork sessions are not migrated.',
    'Live turns, approval dialogs and terminals are not restored.',
]


def safe_path(root: Path, relative: str) -> Path:
    parts = Path(relative).parts
    if not parts or Path(relative).is_absolute() or any(p in {'.', '..'} for p in parts):
        raise ValueError(f'Invalid relative path: {relative}')
    path = root
    for part in ('', *parts):
        path = path / part
        if path.is_symlink():
            raise ValueError(f'Symlink is not supported: {path}')
    return path


def encode(value: dict) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + '\n').encode()


def object_json(raw: bytes) -> dict:
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError('expected a JSON object')
    return data


def valid_record(data: dict, filename: str) -> bool:
    cli_id = data.get('cliSessionId')
    desktop_id = data.get('sessionId')
    return (
        isinstance(cli_id, str) and bool(TOKEN.fullmatch(cli_id))
        and isinstance(desktop_id, str) and bool(re.fullmatch(r'local_[A-Za-z0-9_-]+', desktop_id))
        and filename == f'{desktop_id}.json'
        and isinstance(data.get('cwd'), str) and Path(data['cwd']).is_absolute()
        and type(data.get('lastActivityAt')) in (int, float)
        and math.isfinite(data['lastActivityAt'])
        and ('isArchived' not in data or type(data['isArchived']) is bool)
        and ('schemaVersion' not in data)  # no versioned record format has been verified
        and isinstance(data.get('enabledMcpTools', {}), dict)
        and isinstance(data.get('prs', []), list)
        and all(isinstance(pr, dict) and isinstance(pr.get('repo'), str)
                and type(pr.get('prNumber')) is int for pr in data.get('prs', []))
        and all(data.get(key) is None or isinstance(data[key], str)
                for key in ('originCwd', 'worktreePath', 'postTurnSummaryFor', 'lastAssistantUuid'))
        and (data.get('spawnedFrom') is None or
             isinstance(data['spawnedFrom'], dict) and isinstance(data['spawnedFrom'].get('sessionId'), str))
    )


class SessionStore:
    def __init__(self, root: Path, projects: Path, backups: Path):
        # Canonicalize the selected roots once; children must never be symlinks.
        self.root = root.expanduser().resolve()
        self.projects = projects.expanduser().resolve()
        self.backups = backups.expanduser().resolve()
        if self.backups == self.root or self.root in self.backups.parents:
            raise ValueError('Backups must be outside the session store')
        self.observed: dict[Path, bytes | None] = {}

    def read(self, relative: str) -> bytes | None:
        path = safe_path(self.root, relative)
        raw = path.read_bytes() if path.exists() else None
        if path in self.observed and self.observed[path] != raw:
            raise RuntimeError(f'Session store changed while planning: {relative}; retry with Claude stopped')
        self.observed[path] = raw
        return raw

    def accounts(self) -> list[str]:
        result = []
        if not self.root.exists():
            return result
        for account in sorted(self.root.iterdir()):
            if not TOKEN.fullmatch(account.name):
                continue
            safe_path(self.root, account.name)
            if account.is_dir():
                for org in sorted(account.iterdir()):
                    safe_path(self.root, f'{account.name}/{org.name}')
                    if TOKEN.fullmatch(org.name) and org.is_dir():
                        result.append(f'{account.name}/{org.name}')
        return result

    def select(self, value: str) -> str:
        parts = value.split('/')
        if len(parts) != 2 or not all(TOKEN.fullmatch(p) for p in parts) or value not in self.accounts():
            raise ValueError('Select an existing account/org from `acp claude inventory`')
        return value

    def records(self, account: str, unavailable: list) -> dict:
        result = {}
        for path in sorted((self.root / account).glob('*.json')):
            raw = self.read(f'{account}/{path.name}')
            try:
                data = object_json(raw)
                if not valid_record(data, path.name):
                    raise ValueError('unrecognized identity, version or field type')
                result[data['sessionId']] = (data, raw)
            except (ValueError, TypeError, UnicodeError) as exc:
                unavailable.append({'session': path.stem, 'account': account,
                                    'reason': f'unsupported record: {exc}'})
        return result

    def transcript_index(self) -> dict[str, list[Path]]:
        result = {}
        for path in self.projects.glob('*/*.jsonl'):
            safe_path(self.projects, str(path.relative_to(self.projects)))
            result.setdefault(path.stem, []).append(path)
        return result

    def inspect(self, data: dict, transcripts: dict, unavailable: list) -> dict | None:
        session = data['sessionId']
        def issue(reason):
            unavailable.append({'session': session, 'reason': reason})

        paths = transcripts.get(data['cliSessionId'], [])
        if len(paths) != 1:
            issue('missing transcript' if not paths else 'ambiguous transcript')
            return None
        messages = set()
        try:
            with paths[0].open() as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    message = json.loads(line)
                    if isinstance(message, dict) and isinstance(message.get('uuid'), str):
                        messages.add(message['uuid'])
        except (ValueError, UnicodeError, OSError):
            issue('unreadable or malformed transcript')
            return None
        result = dict(data)
        status_fields = ('postTurnSummary', 'postTurnSummaryFor', 'lastAssistantUuid', 'turnWrapUp')
        pointers = [data.get(k) for k in ('postTurnSummaryFor', 'lastAssistantUuid') if data.get(k)]
        if any(data.get(k) for k in status_fields) and (not pointers or any(p not in messages for p in pointers)):
            for key in status_fields:
                result.pop(key, None)
            issue('status dropped: its message is absent from the transcript')
        for key in ('cwd', 'originCwd', 'worktreePath'):
            location = data.get(key)
            if location and (not Path(location).is_absolute() or not Path(location).is_dir()):
                issue(f'missing {"worktree" if key == "worktreePath" else "directory"}: {key}={location}')
        worktree = data.get('worktreePath')
        if worktree and Path(worktree).is_dir():
            try:
                head = subprocess.run(['git', '-C', worktree, 'symbolic-ref', '--quiet', '--short', 'HEAD'],
                                      capture_output=True, text=True, timeout=5)
                if head.returncode:
                    issue('worktree HEAD detached or unavailable; saved branch preserved')
                elif data.get('branch') and head.stdout.strip() != data['branch']:
                    issue('worktree branch differs from saved branch; saved branch preserved')
            except (OSError, subprocess.TimeoutExpired):
                issue('worktree HEAD could not be checked')
        return result

    def inventory(self) -> dict:
        self.observed = {}
        unavailable, accounts = [], []
        transcripts = self.transcript_index()
        for account in self.accounts():
            start = len(unavailable)
            records = self.records(account, unavailable)
            items = [{'session': item['session'], 'classification': 'unsupported record',
                      'copyable': False, 'issues': [item]} for item in unavailable[start:]]
            for session, (data, _) in records.items():
                start = len(unavailable)
                inspected = self.inspect(data, transcripts, unavailable)
                items.append({'session': session, 'title': data.get('title', session),
                              'classification': 'ready' if len(unavailable) == start else 'unavailable',
                              'copyable': inspected is not None,
                              'issues': unavailable[start:]})
            accounts.append({'account': account, 'count': len(items), 'sessions': items})
        return {'accounts': accounts, 'unavailable': unavailable, 'needs_login_or_ui_check': UI_CHECKS}

    def plan(self, target: str, source: str | None = None) -> tuple[dict, list]:
        self.observed = {}
        target = self.select(target)
        if source is None:
            candidates = [a for a in self.accounts() if a != target and any((self.root / a).glob('local_*.json'))]
            if len(candidates) != 1:
                raise ValueError('Cannot infer source; supply --from account/org')
            source = candidates[0]
        source = self.select(source)
        if source == target:
            raise ValueError('Source and destination must differ')
        report = dict(source=source, destination=target, verdict='partial', restored=[], preserved_as_newer=[],
                      unavailable=[], needs_login_or_ui_check=UI_CHECKS, changes=[], already_present=[])
        unavailable = report['unavailable']
        sources = self.records(source, unavailable)
        destinations = self.records(target, unavailable)
        transcripts = self.transcript_index()
        final = {key: value[0] for key, value in destinations.items()}
        changes = []

        def change(relative, after):
            before = self.read(relative)
            if before != after:
                changes.append({'path': relative, 'before': before, 'after': after})
                report['changes'].append(relative)

        source_tools, destination_tools = set(), set()
        for data, _ in destinations.values():
            destination_tools.update(data.get('enabledMcpTools', {}))
        for session, (original, raw) in sources.items():
            source_tools.update(original.get('enabledMcpTools', {}))
            candidate = self.inspect(original, transcripts, unavailable)
            if candidate is None:
                continue
            relative = f'{target}/{session}.json'
            existing_raw = self.read(relative)
            if existing_raw is not None and session not in destinations:
                unavailable.append({'session': session, 'reason': 'unsupported destination record; preserved'})
                continue
            if session in destinations:
                destination, _ = destinations[session]
                newer_destination = destination['lastActivityAt'] > candidate['lastActivityAt']
                if destination['lastActivityAt'] >= candidate['lastActivityAt']:
                    destination = self.inspect(destination, transcripts, unavailable)
                    if destination is None:
                        continue
                if destination['lastActivityAt'] == candidate['lastActivityAt']:
                    # A prior migration may already have merged PRs or removed stale status.
                    a, b = dict(candidate), dict(destination)
                    a.pop('prs', None)
                    b.pop('prs', None)
                    if a != b:
                        unavailable.append({'session': session, 'reason': 'conflicting records with equal timestamps; preserved destination'})
                        continue
                winner, other = (destination, candidate) if newer_destination else (candidate, destination)
                candidate = dict(winner)
                prs = list(winner.get('prs', []))
                keys = {(p.get('host', 'github.com'), p['repo'], p['prNumber']) for p in prs}
                for pr in other.get('prs', []):
                    key = (pr.get('host', 'github.com'), pr['repo'], pr['prNumber'])
                    if key not in keys:
                        prs.append(pr)
                        keys.add(key)
                if prs:
                    candidate['prs'] = prs
                if newer_destination:
                    report['preserved_as_newer'].append(session)
            final[session] = candidate
            if session in destinations and candidate == destinations[session][0]:
                report['already_present'].append(session)
                continue
            change(relative, raw if candidate == original else encode(candidate))
            report['restored'].append(session)

        report['connector_tools_missing_in_destination'] = sorted(source_tools - destination_tools)
        if report['connector_tools_missing_in_destination']:
            unavailable.append({'reason': 'connector tool IDs absent from destination records; reconnect and check access'})
        self.plan_archive(source, target, final, change, unavailable)
        self.plan_backlog(source, target, final, change, unavailable)
        for session in final:
            seen, current = set(), session
            while current in final:
                if current in seen:
                    unavailable.append({'session': session, 'reason': 'cycle in spawnedFrom parent links'})
                    break
                seen.add(current)
                parent = final[current].get('spawnedFrom')
                if not parent:
                    break
                current = parent['sessionId']
                if current not in final:
                    unavailable.append({'session': session, 'reason': f'missing parent: {current}'})
        return report, changes

    def plan_archive(self, source, target, final, change, unavailable):
        indexes = []
        for account in (source, target):
            raw = self.read(f'{account}/archived-sessions.idx')
            try:
                data = object_json(raw) if raw is not None else {'v': 1, 'archived': []}
                if data.get('v') != 1 or not isinstance(data.get('archived'), list) or not all(
                        isinstance(s, str) for s in data['archived']):
                    raise ValueError('unknown format')
                indexes.append(data)
            except (ValueError, UnicodeError, TypeError):
                unavailable.append({'reason': f'unsupported archive index in {account}; index unchanged'})
                return
        archived = set(indexes[0]['archived']) | set(indexes[1]['archived'])
        dangling = archived - final.keys()
        if dangling:
            unavailable.append({'reason': 'archive references missing records', 'sessions': sorted(dangling)})
        # Keep destination-only references; never erase its unknown/unsupported records.
        destination_only = set(indexes[1]['archived']) - final.keys()
        desired = sorted(destination_only | {s for s, d in final.items() if d.get('isArchived', s in archived)})
        path = f'{target}/archived-sessions.idx'
        if desired != indexes[1]['archived']:
            change(path, encode({**indexes[1], 'archived': desired}))

    def plan_backlog(self, source, target, final, change, unavailable):
        data = []
        for account in (source, target):
            folder = safe_path(self.root, f'{account}/backlog')
            if folder.exists():
                for path in folder.iterdir():
                    safe_path(self.root, str(path.relative_to(self.root)))
                    if path.name != 'tasks.json':
                        unavailable.append({'reason': f'unsupported backlog entry: {account}/backlog/{path.name}'})
            raw = self.read(f'{account}/backlog/tasks.json')
            try:
                value = object_json(raw) if raw is not None else {'version': 1, 'items': []}
                items = value.get('items')
                if value.get('version') != 1 or not isinstance(items, list) or not all(
                        isinstance(i, dict) and isinstance(i.get('id'), str) for i in items):
                    raise ValueError('unknown format')
                if len({i['id'] for i in items}) != len(items):
                    raise ValueError('duplicate IDs')
                data.append(value)
            except (ValueError, TypeError, UnicodeError):
                unavailable.append({'reason': f'unsupported backlog in {account}; backlog unchanged'})
                return
        items = list(data[1]['items'])
        existing = {i['id']: i for i in items}
        for item in data[0]['items']:
            if item['id'] in existing:
                if item != existing[item['id']]:
                    unavailable.append({'reason': f'backlog conflict {item["id"]}; destination preserved'})
            elif item.get('originSessionId') and item['originSessionId'] not in final:
                unavailable.append({'reason': f'backlog {item["id"]} has missing origin session; skipped'})
            else:
                items.append(item)
        if items != data[1]['items']:
            change(f'{target}/backlog/tasks.json', encode({**data[1], 'items': items}))

    def migrate(self, target, source=None, dry_run=False):
        from .claude_migration import apply
        if dry_run:
            report, _ = self.plan(target, source)
            return {**report, 'dry_run': True, 'disk_verified': False}
        return apply(self, target, source)

    def rollback(self, migration_id):
        from .claude_migration import rollback
        return rollback(self, migration_id)

    def status(self):
        from .claude_migration import status
        return status(self)
