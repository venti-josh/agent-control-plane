"""Durable, hash-guarded file transactions for the Claude session store."""
from __future__ import annotations

import fcntl
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from .claude_sessions import TOKEN, encode, object_json, safe_path


def digest(raw: bytes | None) -> str | None:
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def sync_directory(path: Path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, raw: bytes):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.acp-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        sync_directory(path.parent)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def require_stopped():
    """Fail closed if host process visibility is unavailable; never stop an app."""
    if sys.platform != 'darwin':
        raise RuntimeError('Writing Claude Desktop sessions is supported on macOS only')
    if os.environ.get('APP_SANDBOX_CONTAINER_ID'):
        raise RuntimeError('Run migration from a host terminal outside the app sandbox')
    try:
        result = subprocess.run(['/bin/ps', '-axo', 'pid=,comm='], capture_output=True, text=True, timeout=10,
                                check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('Cannot inspect host processes; run from a host terminal') from exc
    rows = [line.strip().split(None, 1) for line in result.stdout.splitlines() if line.strip()]
    if result.returncode or not {str(os.getpid()), '1'} <= {r[0] for r in rows}:
        raise RuntimeError('Cannot verify host process visibility; run from a host terminal')
    for row in rows:
        if len(row) != 2:
            raise RuntimeError('Cannot parse host process list')
        executable = row[1].lower()
        if ('/claude.app/' in executable or Path(executable).name in {'claude', 'claude.exe'}
                or '/share/claude/versions/' in executable):
            raise RuntimeError('Claude is running. Quit Claude Desktop and stop Claude CLI/background sessions first.')
    # Older npm installations run under node rather than a native claude binary.
    try:
        result = subprocess.run(['/bin/ps', '-axo', 'args='], capture_output=True, text=True, timeout=10,
                                check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError('Cannot inspect Claude CLI processes') from exc
    if result.returncode:
        raise RuntimeError('Cannot inspect Claude CLI processes')
    if any(re.search(r'(?:^|\s)\S*(?:@anthropic-ai/claude-code/|/claude-code/cli\.js)', line)
           for line in result.stdout.splitlines()):
        raise RuntimeError('Claude CLI is running. Stop it before migrating.')


@contextmanager
def migration_lock(store):
    path = safe_path(store.root, '.acp-migration.lock')
    # Do not unlink a flock file: a second process could otherwise lock a new inode.
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RuntimeError('Another Claude migration or rollback is running') from exc
        yield
    finally:
        os.close(fd)


def read_current(store, relative):
    path = safe_path(store.root, relative)
    return path.read_bytes() if path.exists() else None


def migration_order(store):
    path = safe_path(store.root, '.acp-migration-order.json')
    data = object_json(path.read_bytes()) if path.exists() else {'version': 1, 'active': []}
    active = data.get('active')
    if data.get('version') != 1 or not isinstance(active, list) or not all(
            isinstance(item, dict) and isinstance(item.get('id'), str) and TOKEN.fullmatch(item['id'])
            and isinstance(item.get('backup_directory'), str) for item in active):
        raise ValueError('Invalid store-wide migration order; rollback refused')
    return data


def save_order(store, order):
    atomic_write(safe_path(store.root, '.acp-migration-order.json'), encode(order))


def apply(store, target, source, expected_plan=None):
    # Resolve selectors before creating even a lock file.
    store.select(target)
    if source is not None:
        store.select(source)
        if source == target:
            raise ValueError('Source and destination must differ')
    require_stopped()
    with migration_lock(store):
        order = migration_order(store)
        report, changes = store.plan(target, source)
        if expected_plan is not None and (report, changes) != expected_plan:
            raise RuntimeError('Session store changed since preview; rerun migration to review a fresh plan')
        if not changes:
            return {**report, 'dry_run': False, 'disk_verified': True, 'migration_id': None}
        migration_id = f'{time.time_ns()}-{uuid.uuid4().hex[:8]}'
        store.backups.mkdir(parents=True, mode=0o700, exist_ok=True)
        folder = safe_path(store.backups, migration_id)
        folder.mkdir(mode=0o700)
        sync_directory(store.backups)
        manifest = dict(version=1, id=migration_id, root=str(store.root), destination=target,
                        source=report['source'], state='prepared', created_at=time.time(), entries=[], report=report)
        for index, change in enumerate(changes):
            path = safe_path(store.root, change['path'])
            if change['before'] is not None:
                atomic_write(folder / f'{index}.before', change['before'])
            atomic_write(folder / f'{index}.after', change['after'])
            manifest['entries'].append(dict(path=change['path'], before=digest(change['before']),
                                            after=digest(change['after']),
                                            mode=path.stat().st_mode & 0o777 if path.exists() else None))
        # Persist the write-ahead manifest BEFORE changing any Claude files.
        atomic_write(folder / 'manifest.json', encode(manifest))
        try:
            # Track dependency order in the shared store, not a particular backup
            # directory. Later migrations may depend on unchanged earlier files.
            order['active'].append({'id': migration_id, 'backup_directory': str(store.backups)})
            save_order(store, order)
            require_stopped()
            for path, expected in store.observed.items():
                if read_current(store, str(path.relative_to(store.root))) != expected:
                    raise RuntimeError('Session store changed while planning; rollback then retry')
            for change in changes:
                require_stopped()
                if read_current(store, change['path']) != change['before']:
                    raise RuntimeError(f'Session changed before write: {change["path"]}')
                atomic_write(safe_path(store.root, change['path']), change['after'])
            if any(read_current(store, c['path']) != c['after'] for c in changes):
                raise RuntimeError('Post-write hash verification failed')
            manifest['state'] = 'complete'
            atomic_write(folder / 'manifest.json', encode(manifest))
        except Exception as exc:
            manifest['state'] = 'failed'
            try:
                atomic_write(folder / 'manifest.json', encode(manifest))
            except OSError:
                pass  # the prepared journal remains sufficient for rollback
            raise RuntimeError(f'Migration {migration_id} interrupted: {exc}. '
                               f'Run `acp claude rollback {migration_id}` with the same directory options.') from exc
        return {**report, 'dry_run': False, 'disk_verified': True,
                'migration_id': migration_id, 'backup': str(folder)}


def load_manifest(store, migration_id):
    if not TOKEN.fullmatch(migration_id):
        raise ValueError('Invalid migration ID')
    path = safe_path(store.backups, f'{migration_id}/manifest.json')
    data = object_json(path.read_bytes())
    if data.get('version') != 1 or data.get('id') != migration_id or data.get('root') != str(store.root):
        raise ValueError('Migration manifest does not match this session store')
    destination = store.select(data.get('destination', ''))
    entries = data.get('entries')
    if not isinstance(entries, list):
        raise ValueError('Invalid migration entries')
    names = set()
    for entry in entries:
        relative = entry.get('path', '')
        safe_path(store.root, relative)
        suffix = relative.removeprefix(destination + '/')
        if (not relative.startswith(destination + '/') or relative in names or
                not (suffix in {'archived-sessions.idx', 'backlog/tasks.json'} or
                     re.fullmatch(r'local_[A-Za-z0-9_-]+\.json', suffix))):
            raise ValueError('Invalid migration target path')
        names.add(relative)
        if not isinstance(entry.get('after'), str) or not re.fullmatch('[a-f0-9]{64}', entry['after']):
            raise ValueError('Invalid after hash')
        if entry.get('before') is not None and (not isinstance(entry['before'], str) or
                                                not re.fullmatch('[a-f0-9]{64}', entry['before'])):
            raise ValueError('Invalid before hash')
        mode = entry.get('mode')
        if mode is not None and (type(mode) is not int or mode < 0 or mode > 0o777):
            raise ValueError('Invalid file mode')
    return data


def rollback(store, migration_id):
    require_stopped()
    with migration_lock(store):
        manifest = load_manifest(store, migration_id)
        folder = safe_path(store.backups, migration_id)
        order = migration_order(store)
        latest = order['active'][-1] if order['active'] else None
        identity = {'id': migration_id, 'backup_directory': str(store.backups)}
        if manifest['state'] == 'rolled_back' or (identity not in order['active'] and all(
                digest(read_current(store, e['path'])) == e['before'] for e in manifest['entries'])):
            # Registration may have failed before any session writes, or rollback
            # completed before its order entry could be removed. Neither needs a
            # file mutation; repeated rollback must not disturb subsequent work.
            if manifest['state'] != 'rolled_back':
                manifest['state'] = 'rolled_back'
                atomic_write(folder / 'manifest.json', encode(manifest))
            if latest == identity:
                order['active'].pop()
                save_order(store, order)
            return dict(migration_id=migration_id, state='rolled_back', restored=[],
                        skipped_changed_files=[], backup=str(folder))
        if latest != identity:
            raise RuntimeError(f'Rollback requires the latest migration first: {latest}')
        # Validate ALL required backup blobs before restoring the first file.
        originals = []
        for index, entry in enumerate(manifest['entries']):
            original = None
            if entry['before'] is not None:
                original = safe_path(folder, f'{index}.before').read_bytes()
                if digest(original) != entry['before']:
                    raise ValueError('Backup hash mismatch; rollback refused')
            originals.append(original)
        skipped, restored = [], []
        for entry, original in reversed(list(zip(manifest['entries'], originals))):
            require_stopped()
            current = read_current(store, entry['path'])
            if digest(current) == entry['before']:
                continue  # already restored, or a prepared write never happened
            if digest(current) != entry['after']:
                skipped.append(entry['path'])
                continue
            path = safe_path(store.root, entry['path'])
            if original is None:
                path.unlink()
                sync_directory(path.parent)
            else:
                atomic_write(path, original)
                if entry['mode'] is not None:
                    path.chmod(entry['mode'])
            restored.append(entry['path'])
        manifest['state'] = 'rollback_partial' if skipped else 'rolled_back'
        manifest['rollback_skipped'] = skipped
        atomic_write(folder / 'manifest.json', encode(manifest))
        if not skipped:
            order['active'].pop()
            save_order(store, order)
        return dict(migration_id=migration_id, state=manifest['state'], restored=restored,
                    skipped_changed_files=skipped, backup=str(folder))


def status(store):
    locked = False
    lock = safe_path(store.root, '.acp-migration.lock')
    if lock.exists():
        with lock.open('rb') as stream:
            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                locked = True
    migrations, incomplete = [], []
    if store.backups.exists():
        for folder in sorted(store.backups.iterdir()):
            safe_path(store.backups, folder.name)
            if not folder.is_dir():
                continue
            path = safe_path(store.backups, f'{folder.name}/manifest.json')
            if not path.exists():
                incomplete.append(folder.name)
                continue
            data = object_json(path.read_bytes())
            if data.get('root') == str(store.root):
                migrations.append({k: data.get(k) for k in ('id', 'state', 'source', 'destination', 'created_at')})
    order = migration_order(store)
    return dict(locked=locked, backup_directory=str(store.backups), migrations=migrations,
                latest_rollback=order['active'][-1] if order['active'] else None,
                last_migration=migrations[-1] if migrations else None, unprepared_backups=incomplete)
