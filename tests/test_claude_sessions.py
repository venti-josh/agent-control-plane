"""Migration contracts use isolated app stores, never the user's Claude data."""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from acp import claude_sessions as sessions


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    return path


@pytest.fixture
def store(tmp_path, monkeypatch):
    root = tmp_path / 'Claude' / 'claude-code-sessions'
    projects = tmp_path / 'claude' / 'projects'
    backups = tmp_path / 'backups'
    for name in ('account-a/org-a', 'account-b/org-b'):
        (root / name).mkdir(parents=True)
    monkeypatch.setattr('acp.claude_migration.require_stopped', lambda: None)
    return sessions.SessionStore(root, projects, backups)


def record(store, name='one', org='account-a/org-a', **fields):
    data = dict(sessionId=f'local_{name}', cliSessionId=name, cwd=str(store.root.parent),
                createdAt=1, lastActivityAt=100, title=name, isArchived=False,
                unknownFutureField={'preserve': True})
    data.update(fields)
    write(store.projects / '-repo' / f'{name}.jsonl', {'type': 'assistant', 'uuid': 'message-one'})
    return write(store.root / org / f'local_{name}.json', data)


def migrate(store, **kwargs):
    return store.migrate('account-b/org-b', **kwargs)


def tree(path):
    return {str(p.relative_to(path)): p.read_bytes() for p in path.rglob('*') if p.is_file()}


def test_inventory_and_dry_run_are_read_only_and_name_unavailable_records(store):
    source = record(store, cwd='/does/not/exist')
    missing = record(store, 'missing')
    (store.projects / '-repo/missing.jsonl').unlink()
    before = tree(store.root.parent.parent)
    inventory = store.inventory()
    assert len(inventory['accounts']) == 2
    report = migrate(store, dry_run=True)
    assert report['dry_run'] is True and report['verdict'] == 'partial'
    assert any(x['session'] == source.stem and 'missing directory' in x['reason'] for x in report['unavailable'])
    assert any(x['session'] == missing.stem and 'missing transcript' in x['reason'] for x in report['unavailable'])
    assert report['needs_login_or_ui_check']
    assert inventory['unread_state']['status'] == 'not_migrated'
    assert report['unread_state']['status'] == 'not_migrated'
    assert 'blue unread dots' in report['unread_state']['reason']
    assert tree(store.root.parent.parent) == before


def test_copy_preserves_identity_unknown_fields_and_rollback_bytes(store):
    source = record(store, postTurnSummary={'text': 'summary'}, postTurnSummaryFor='message-one',
                    lastAssistantUuid='message-one', turnWrapUp={'description': 'done'})
    before = tree(store.root / 'account-b/org-b')
    report = migrate(store)
    destination = store.root / 'account-b/org-b' / source.name
    assert destination.read_bytes() == source.read_bytes()
    assert report['disk_verified'] is True
    assert report['unread_state']['status'] == 'not_migrated'
    assert migrate(store)['changes'] == []
    assert store.migrate('account-a/org-a')['changes'] == []
    assert store.status()['last_migration']['id'] == report['migration_id']
    rolled_back = store.rollback(report['migration_id'])
    assert rolled_back['state'] == 'rolled_back'
    assert tree(store.root / 'account-b/org-b') == before
    assert source.exists()


@pytest.mark.parametrize('source_time,destination_time,winner', [(200, 100, 'source'), (100, 200, 'destination')])
def test_conflicts_choose_newer_and_merge_prs_without_losing_destination_backup(store, source_time, destination_time, winner):
    source = record(store, lastActivityAt=source_time, title='source',
                    prs=[{'repo': 'o/r', 'prNumber': 1, 'state': 'OPEN'}])
    destination = record(store, org='account-b/org-b', lastActivityAt=destination_time, title='destination',
                         prs=[{'repo': 'o/r', 'prNumber': 2, 'state': 'OPEN'}])
    original = destination.read_bytes()
    result = migrate(store)
    data = json.loads(destination.read_text())
    assert data['title'] == winner
    assert {pr['prNumber'] for pr in data['prs']} == {1, 2}
    assert source.exists()
    assert migrate(store)['changes'] == []
    store.rollback(result['migration_id'])
    assert destination.read_bytes() == original


def test_equal_time_different_records_are_reported_without_overwrite(store):
    record(store, title='source')
    destination = record(store, org='account-b/org-b', title='destination')
    before = destination.read_bytes()
    report = migrate(store)
    assert report['changes'] == []
    assert any('equal' in x['reason'] for x in report['unavailable'])
    assert destination.read_bytes() == before


def test_invalid_status_is_dropped_and_broken_parent_is_reported(store):
    source = record(store, postTurnSummaryFor='gone', postTurnSummary={'text': 'stale'},
                    lastAssistantUuid='gone', turnWrapUp={'description': 'stale'},
                    spawnedFrom={'sessionId': 'local_missing'})
    report = migrate(store)
    saved = json.loads((store.root / 'account-b/org-b' / source.name).read_text())
    assert not {'postTurnSummary', 'postTurnSummaryFor', 'lastAssistantUuid', 'turnWrapUp'} & saved.keys()
    assert any('status dropped' in x['reason'] for x in report['unavailable'])
    assert any('parent' in x['reason'] for x in report['unavailable'])


def test_archive_and_backlog_merge_preserve_destination_items_and_rollback(store):
    record(store, isArchived=True)
    record(store, 'existing', org='account-b/org-b', isArchived=True)
    source = store.root / 'account-a/org-a'
    destination = store.root / 'account-b/org-b'
    write(source / 'archived-sessions.idx', {'v': 1, 'archived': ['local_one', 'local_missing']})
    write(destination / 'archived-sessions.idx', {'v': 1, 'archived': ['local_existing'], 'extra': True})
    write(source / 'backlog/tasks.json', {'version': 1, 'items': [{'id': 'new'}, {'id': 'same', 'title': 'source'}]})
    write(destination / 'backlog/tasks.json', {'version': 1, 'items': [{'id': 'same', 'title': 'destination'}]})
    before = tree(destination)
    report = migrate(store)
    archive = json.loads((destination / 'archived-sessions.idx').read_text())
    assert archive == {'v': 1, 'archived': ['local_existing', 'local_one'], 'extra': True}
    backlog = json.loads((destination / 'backlog/tasks.json').read_text())
    assert backlog['items'] == [{'id': 'same', 'title': 'destination'}, {'id': 'new'}]
    assert any('backlog' in x['reason'] for x in report['unavailable'])
    assert migrate(store)['changes'] == []
    store.rollback(report['migration_id'])
    assert tree(destination) == before


def test_rollback_preserves_post_migration_work_and_can_be_retried(store):
    record(store)
    record(store, 'two')
    report = migrate(store)
    destination = store.root / 'account-b/org-b/local_one.json'
    destination.write_text('{"new": "work"}')
    rolled_back = store.rollback(report['migration_id'])
    assert rolled_back['state'] == 'rollback_partial'
    assert destination.read_text() == '{"new": "work"}'
    assert not (destination.parent / 'local_two.json').exists()
    assert store.rollback(report['migration_id'])['state'] == 'rollback_partial'


def test_rollback_requires_reverse_order_across_backup_directories(store):
    record(store, isArchived=True)
    first = migrate(store)
    record(store, 'two', isArchived=True)
    other = sessions.SessionStore(store.root, store.projects, store.backups / 'other')
    second = migrate(other)
    before = tree(store.root)
    with pytest.raises(RuntimeError, match='latest migration'):
        store.rollback(first['migration_id'])
    assert tree(store.root) == before
    assert other.rollback(second['migration_id'])['state'] == 'rolled_back'
    assert store.rollback(first['migration_id'])['state'] == 'rolled_back'
    assert tree(store.root / 'account-b/org-b') == {}
    assert other.rollback(second['migration_id'])['state'] == 'rolled_back'


@pytest.mark.parametrize('failure_point', ['register', 'pop'])
def test_migration_order_write_failure_is_recoverable(store, monkeypatch, failure_point):
    from acp import claude_migration
    record(store)
    original = claude_migration.save_order

    def fail_order(*args):
        raise OSError('disk full')

    if failure_point == 'register':
        monkeypatch.setattr(claude_migration, 'save_order', fail_order)
        with pytest.raises(RuntimeError, match='rollback'):
            migrate(store)
        migration_id = store.status()['last_migration']['id']
    else:
        migration_id = migrate(store)['migration_id']
        monkeypatch.setattr(claude_migration, 'save_order', fail_order)
        with pytest.raises(OSError, match='disk full'):
            store.rollback(migration_id)
    assert tree(store.root / 'account-b/org-b') == {}
    monkeypatch.setattr(claude_migration, 'save_order', original)
    assert store.rollback(migration_id)['state'] == 'rolled_back'
    assert store.status()['latest_rollback'] is None


def test_interrupted_write_has_durable_manifest_and_can_be_rolled_back(store, monkeypatch):
    from acp import claude_migration
    record(store)
    record(store, 'two')
    original = claude_migration.atomic_write
    count = 0

    def interrupt(path, data):
        nonlocal count
        if path.parent == store.root / 'account-b/org-b':
            count += 1
            if count == 2:
                raise OSError('disk full')
        original(path, data)

    monkeypatch.setattr(claude_migration, 'atomic_write', interrupt)
    with pytest.raises(RuntimeError, match='rollback'):
        migrate(store)
    status = store.status()['last_migration']
    assert status['state'] == 'failed'
    assert len(list((store.root / 'account-b/org-b').glob('*.json'))) == 1
    monkeypatch.setattr(claude_migration, 'atomic_write', original)
    assert store.rollback(status['id'])['state'] == 'rolled_back'
    assert tree(store.root / 'account-b/org-b') == {}


def test_process_guard_blocks_apply_and_rollback_but_not_dry_run(store, monkeypatch):
    record(store)
    result = migrate(store)
    def running():
        raise RuntimeError('Claude is running')
    monkeypatch.setattr('acp.claude_migration.require_stopped', running)
    assert migrate(store, dry_run=True)['dry_run'] is True
    with pytest.raises(RuntimeError, match='Claude is running'):
        migrate(store)
    with pytest.raises(RuntimeError, match='Claude is running'):
        store.rollback(result['migration_id'])


def test_ambiguous_source_requires_explicit_selection(store):
    record(store)
    record(store, org='account-c/org-c')
    with pytest.raises(ValueError, match='--from'):
        migrate(store, dry_run=True)
    assert migrate(store, source='account-a/org-a', dry_run=True)['source'] == 'account-a/org-a'


@pytest.mark.parametrize('target', ['../org-b', '/account-b/org-b', 'account-b/../org-b', 'account-a/org-a', 'unknown/org'])
def test_invalid_or_same_destination_never_writes(store, target):
    record(store)
    before = tree(store.root)
    with pytest.raises(ValueError):
        store.migrate(target, source='account-a/org-a')
    assert tree(store.root) == before


def test_symlink_record_is_rejected_without_reading_or_overwriting_target(store, tmp_path):
    record(store)
    external = tmp_path / 'external.json'
    external.write_text('do not touch')
    (store.root / 'account-b/org-b/local_one.json').symlink_to(external)
    with pytest.raises(ValueError, match='[Ss]ymlink'):
        migrate(store)
    assert external.read_text() == 'do not touch'


def test_unsupported_records_and_indexes_are_reported_not_reinterpreted(store):
    record(store, sessionId='local_wrong')
    write(store.root / 'account-a/org-a/archived-sessions.idx', {'v': 999, 'archived': []})
    result = migrate(store, dry_run=True)
    assert result['changes'] == []
    assert any('unsupported record' in x['reason'] for x in result['unavailable'])
    assert any('archive' in x['reason'] for x in result['unavailable'])


def test_cli_inventory_works_without_server_or_creating_acp_home(store, tmp_path):
    record(store)
    env = {**os.environ, 'ACP_HOME': str(tmp_path / 'unused-home')}
    result = subprocess.run([sys.executable, '-m', 'acp.cli', 'claude', 'inventory',
                             '--sessions-dir', str(store.root), '--projects-dir', str(store.projects),
                             '--backup-dir', str(store.backups)], capture_output=True, text=True, env=env)
    assert result.returncode == 0, result.stderr
    assert len(json.loads(result.stdout)['accounts']) == 2
    assert not (tmp_path / 'unused-home').exists()


def test_imported_desktop_id_can_differ_from_cli_transcript_id(store):
    original = record(store, cliSessionId='original-cli-id')
    write(store.projects / '-repo/original-cli-id.jsonl', {'uuid': 'message-one'})
    result = migrate(store)
    assert result['restored'] == ['local_one']
    assert (store.root / 'account-b/org-b/local_one.json').read_bytes() == original.read_bytes()


def test_newer_destination_stale_status_is_cleaned_when_merged(store):
    record(store, lastActivityAt=100)
    destination = record(store, org='account-b/org-b', lastActivityAt=200,
                         lastAssistantUuid='gone', turnWrapUp={'description': 'stale'})
    result = migrate(store)
    assert 'lastAssistantUuid' not in json.loads(destination.read_text())
    assert any('status dropped' in x['reason'] for x in result['unavailable'])


def test_identical_records_with_stale_status_are_cleaned_without_equal_time_conflict(store):
    source = record(store, lastAssistantUuid='gone', turnWrapUp={'description': 'stale'})
    destination = record(store, org='account-b/org-b', lastAssistantUuid='gone',
                         turnWrapUp={'description': 'stale'})
    original = destination.read_bytes()
    result = migrate(store)
    assert 'lastAssistantUuid' not in json.loads(destination.read_text())
    assert not any('equal timestamps' in x['reason'] for x in result['unavailable'])
    assert migrate(store)['changes'] == []
    assert source.read_bytes() == original
    store.rollback(result['migration_id'])
    assert destination.read_bytes() == original


def test_corrupt_backup_prevents_any_rollback_writes(store):
    record(store, lastActivityAt=200)
    record(store, org='account-b/org-b', lastActivityAt=100)
    record(store, 'two')
    result = migrate(store)
    (Path(result['backup']) / '0.before').write_text('corrupt')
    before = tree(store.root)
    with pytest.raises(ValueError, match='hash mismatch'):
        store.rollback(result['migration_id'])
    assert tree(store.root) == before


def test_concurrent_migrations_share_store_lock_even_with_different_backup_dirs(store):
    from acp.claude_migration import migration_lock
    record(store)
    other = sessions.SessionStore(store.root, store.projects, store.backups / 'other')
    with migration_lock(store):
        assert other.status()['locked'] is True
        with pytest.raises(RuntimeError, match='Another Claude migration'):
            migrate(other)
    assert not list((store.root / 'account-b/org-b').glob('*.json'))


def test_destination_change_after_plan_is_not_overwritten(store, monkeypatch):
    record(store)
    plan = store.plan
    destination = store.root / 'account-b/org-b/local_one.json'
    def change_after_plan(*args):
        result = plan(*args)
        destination.write_text('new user work')
        return result
    monkeypatch.setattr(store, 'plan', change_after_plan)
    with pytest.raises(RuntimeError, match='changed while planning'):
        migrate(store)
    assert destination.read_text() == 'new user work'
    rolled = store.rollback(store.status()['last_migration']['id'])
    assert rolled['state'] == 'rollback_partial'
    assert destination.read_text() == 'new user work'


def test_destination_change_during_plan_is_not_overwritten(store, monkeypatch):
    record(store, lastActivityAt=200, title='source')
    destination = record(store, org='account-b/org-b', lastActivityAt=100, title='destination')
    inspect = store.inspect
    edited = json.loads(destination.read_text())
    edited.update(lastActivityAt=300, title='new work during planning')

    def edit_during_inspection(*args):
        write(destination, edited)
        return inspect(*args)

    monkeypatch.setattr(store, 'inspect', edit_during_inspection)
    with pytest.raises(RuntimeError, match='changed while planning'):
        migrate(store)
    assert json.loads(destination.read_text()) == edited
    assert store.status()['last_migration'] is None
    monkeypatch.setattr(store, 'inspect', inspect)
    inventory = store.inventory()
    destination_account = next(a for a in inventory['accounts'] if a['account'] == 'account-b/org-b')
    assert destination_account['sessions'][0]['title'] == edited['title']


@pytest.mark.parametrize('executable', ['/Applications/Claude.app/Contents/MacOS/Claude',
                                       '/usr/local/bin/claude', '/Users/test/.local/share/claude/versions/2.1.0'])
def test_host_process_check_recognizes_desktop_and_native_cli(monkeypatch, executable):
    from acp import claude_migration as migration
    monkeypatch.setattr(migration.sys, 'platform', 'darwin')
    monkeypatch.delenv('APP_SANDBOX_CONTAINER_ID', raising=False)
    output = f'1 /sbin/launchd\n{os.getpid()} python\n123 {executable}\n'
    monkeypatch.setattr(migration.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, output, ''))
    with pytest.raises(RuntimeError, match='Claude is running'):
        migration.require_stopped()


def test_host_process_check_refuses_invisible_or_failed_process_table(monkeypatch):
    from acp import claude_migration as migration
    monkeypatch.setattr(migration.sys, 'platform', 'darwin')
    monkeypatch.delenv('APP_SANDBOX_CONTAINER_ID', raising=False)
    monkeypatch.setattr(migration.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, 0, '', ''))
    with pytest.raises(RuntimeError, match='visibility'):
        migration.require_stopped()


def test_parent_cycles_and_detached_worktree_are_reported_without_rewriting_links(store, tmp_path):
    repo = tmp_path / 'repo'
    subprocess.run(['git', 'init', '-q', str(repo)], check=True)
    subprocess.run(['git', '-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.com',
                    'commit', '--allow-empty', '-qm', 'fixture'], check=True)
    subprocess.run(['git', '-C', str(repo), 'checkout', '--detach', '-q'], check=True)
    record(store, worktreePath=str(repo), spawnedFrom={'sessionId': 'local_two'})
    record(store, 'two', spawnedFrom={'sessionId': 'local_one'})
    report = migrate(store)
    assert any('cycle' in x['reason'] for x in report['unavailable'])
    assert any('detached' in x['reason'] for x in report['unavailable'])
    assert json.loads((store.root / 'account-b/org-b/local_one.json').read_text())['spawnedFrom'] == {'sessionId': 'local_two'}


def test_tampered_manifest_cannot_target_files_outside_destination(store, tmp_path):
    record(store)
    result = migrate(store)
    external = tmp_path / 'unrelated'
    external.write_text('preserve')
    manifest_path = Path(result['backup']) / 'manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['entries'][0]['path'] = str(external)
    write(manifest_path, manifest)
    with pytest.raises(ValueError, match='relative path'):
        store.rollback(result['migration_id'])
    assert external.read_text() == 'preserve'


def test_failure_publishing_completion_still_leaves_rollback_journal(store, monkeypatch):
    from acp import claude_migration
    record(store)
    original = claude_migration.atomic_write
    def fail_completion(path, raw):
        if path.name == 'manifest.json' and json.loads(raw)['state'] in {'complete', 'failed'}:
            raise OSError('disk full')
        original(path, raw)
    monkeypatch.setattr(claude_migration, 'atomic_write', fail_completion)
    with pytest.raises(RuntimeError, match='rollback'):
        migrate(store)
    last = store.status()['last_migration']
    assert last['state'] == 'prepared'
    assert (store.root / 'account-b/org-b/local_one.json').exists()
    monkeypatch.setattr(claude_migration, 'atomic_write', original)
    assert store.rollback(last['id'])['state'] == 'rolled_back'


def test_backlog_symlink_is_rejected_before_writing_any_session(store, tmp_path):
    record(store)
    external = tmp_path / 'outside'
    external.mkdir()
    (store.root / 'account-b/org-b/backlog').symlink_to(external)
    with pytest.raises(ValueError, match='Symlink'):
        migrate(store)
    assert not list(external.iterdir())
    assert not list((store.root / 'account-b/org-b').glob('*.json'))
