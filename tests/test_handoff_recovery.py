import asyncio
from uuid import uuid4

import pytest

from acp.desktop import DesktopBridge
from acp.service import TaskService, DomainError
from acp.store import Store
from acp.worker import Worker


def dispatched(service, tmp_path):
    bridge = DesktopBridge(service)
    bridge.connect(str(uuid4()))
    task = service.create_task('Handoff', provider='codex', state='todo', repo_path=str(tmp_path))
    run = service.claim_next()
    bridge.dispatch(run['id'])
    return bridge, task, run


async def test_prepare_grants_send_once_and_claim_recovers_before_prepare(service, tmp_path):
    bridge, task, run = dispatched(service, tmp_path)
    assert bridge.claim(run['id'])['action'] == 'create'
    reopened = TaskService(Store(service.settings.db_path), service.settings)
    restored = DesktopBridge(reopened)
    assert restored.claim(run['id'])['action'] == 'create'
    first = await restored.prepare(run['id'])
    assert first['action'] == 'send'
    assert f"ACP handoff {run['id']}" in first['prompt']
    assert restored.claim(run['id'])['action'] == 'reconcile'
    repeated = await restored.prepare(run['id'])
    assert repeated['action'] == 'reconcile'
    assert 'prompt' not in repeated
    assert len(service.get_task(task['id'])['runs']) == 1


async def test_resume_without_delivery_is_reconciled_not_observed(service, tmp_path):
    bridge, task, run = dispatched(service, tmp_path)
    bridge.claim(run['id']); session = str(uuid4()); bridge.started(run['id'], session)
    bridge.action(run['id'], session, 'input', text='Continue?'); bridge.complete(run['id'], session)
    service.add_comment(task['id'], 'Approved', resume=True,
                        in_reply_to=service.get_task(task['id'])['blocker_comment_id'])
    following = service.claim_next(); bridge.dispatch(following['id'])
    assert bridge.claim(following['id'])['action'] == 'resume'
    assert (await bridge.prepare(following['id']))['action'] == 'send'
    action = bridge.claim(following['id'])
    assert action['action'] == 'reconcile'
    assert action['session_id'] == session
    bridge.started(following['id'], session)
    assert bridge.claim(following['id'])['action'] == 'observe'


async def test_worker_restart_reattaches_same_active_desktop_run(service, tmp_path):
    bridge, task, run = dispatched(service, tmp_path)
    bridge.claim(run['id']); session = str(uuid4()); bridge.started(run['id'], session)
    fake = tmp_path / 'codex'
    fake.write_text('#!/bin/sh\necho Queued\n'); fake.chmod(0o755)
    service.settings.codex_command = str(fake)
    worker = Worker(service)
    await worker.start()
    try:
        await asyncio.sleep(.1)
        assert service.get_task(task['id'])['runs'][0]['status'] == 'running'
        assert bridge.claim(run['id'])['action'] == 'observe'
    finally:
        await worker.stop()
    assert service.get_task(task['id'])['runs'][0]['status'] == 'running'
    restarted = Worker(TaskService(Store(service.settings.db_path), service.settings))
    await restarted.start()
    try:
        bridge.action(run['id'], session, 'review', text='Recovered')
        bridge.complete(run['id'], session)
        await asyncio.sleep(.05)
        current = service.get_task(task['id'])
        assert len(current['runs']) == 1
        assert current['runs'][0]['status'] == 'succeeded'
        assert current['session_id'] == session
    finally:
        await restarted.stop()


def test_restart_never_revives_cancelled_or_failed_desktop_run(service, tmp_path):
    bridge, task, run = dispatched(service, tmp_path)
    service.cancel(task['id']); service.finish_run(run['id'])
    service.recover()
    assert service.get_task(task['id'])['runs'][0]['status'] == 'cancelled'
    with pytest.raises(DomainError):
        bridge.claim(run['id'])


async def test_recovery_can_wake_reconciliation_without_replaying_prompt(service, tmp_path):
    bridge, task, run = dispatched(service, tmp_path)
    fake = tmp_path / 'codex'; fake.write_text('#!/bin/sh\necho Queued\n'); fake.chmod(0o755)
    service.settings.codex_command = str(fake)
    await bridge.queue(run['id']); bridge.claim(run['id']); await bridge.prepare(run['id'])
    assert (await bridge.recover(run['id']))['queued']
    assert bridge.claim(run['id'])['action'] == 'reconcile'
    assert len(service.get_task(task['id'])['runs']) == 1


async def test_queue_transport_retries_are_bounded_and_manual_retry_reuses_run(service, tmp_path):
    from acp.desktop import DesktopRunner
    bridge, task, run = dispatched(service, tmp_path)
    fake = tmp_path / 'codex'
    count = tmp_path / 'attempts'
    fake.write_text(f'#!/bin/sh\necho attempt >> "{count}"\nexit 1\n'); fake.chmod(0o755)
    service.settings.codex_command = str(fake)
    # Force only persisted retry deadlines forward; never bypass the runner or queue subprocess.
    pending = asyncio.create_task(DesktopRunner().run(service, task, run))
    try:
        for _ in range(150):
            entry = service.store.one('SELECT * FROM desktop_runs WHERE run_id=?', (run['id'],))
            if entry.get('delivery_attempts', 0) >= 3 and entry.get('queue_error') and not entry.get('queue_inflight'):
                break
            if entry.get('next_attempt_at'):
                with service.store.transaction() as db:
                    db.execute('UPDATE desktop_runs SET next_attempt_at=0 WHERE run_id=?', (run['id'],))
            await asyncio.sleep(.02)
        assert count.read_text().splitlines() == ['attempt'] * 3
        assert not pending.done()
        await asyncio.sleep(.1)
        assert count.read_text().splitlines() == ['attempt'] * 3
        fake.write_text('#!/bin/sh\necho Queued\n')
        assert (await bridge.recover(run['id']))['queued']
        assert service.store.one('SELECT queue_error FROM desktop_runs WHERE run_id=?', (run['id'],))['queue_error'] is None
        assert len(service.get_task(task['id'])['runs']) == 1
    finally:
        service.cancel(task['id'])
        await asyncio.gather(pending, return_exceptions=True)


def test_started_callback_cannot_reset_start_time(service):
    task = service.create_task('Started once', state='todo')
    run = service.claim_next(); service.mark_started(run['id'])
    first = service.get_task(task['id'])['runs'][0]['started_at']
    service.mark_started(run['id'])
    assert service.get_task(task['id'])['runs'][0]['started_at'] == first
    assert len(service.store.all("SELECT * FROM events WHERE type='run.started'")) == 1


def test_health_exposes_unresolved_handoff_instead_of_claiming_ready(client, tmp_path):
    service = client.app.state.service
    bridge, task, run = dispatched(service, tmp_path)
    bridge.claim(run['id'])
    with service.store.transaction() as db:
        db.execute('UPDATE desktop_runs SET send_started_at=1,claimed_at=1 WHERE run_id=?', (run['id'],))
    health = client.get('/api/health').json()
    assert health['desktop']['state'] == 'needs_reconciliation'
    assert health['desktop']['waiting_runs'] == 1
    assert health['desktop']['oldest_waiting_seconds'] >= 0
    assert health['desktop']['requires_attention']
    assert 'build_revision' in health
    service.cancel(task['id']); service.finish_run(run['id'])
    # Revoking the run does not prove the external chat stopped.
    assert client.get('/api/desktop').json()['state'] == 'needs_reconciliation'


def test_health_reports_exhausted_delivery_with_same_run_id(client, tmp_path):
    service = client.app.state.service
    bridge, task, run = dispatched(service, tmp_path)
    with service.store.transaction() as db:
        db.execute("UPDATE desktop_runs SET delivery_attempts=3,queue_error='Connection failed' WHERE run_id=?", (run['id'],))
    state = client.get('/api/desktop').json()
    assert state['state'] == 'delivery_failed'
    assert state['attention_run_ids'] == [run['id']]


def test_completion_and_run_settlement_commit_together(service, tmp_path, monkeypatch):
    bridge, task, run = dispatched(service, tmp_path)
    bridge.claim(run['id']); session = str(uuid4()); bridge.started(run['id'], session)
    bridge.action(run['id'], session, 'review', text='Verified')
    original = service._event
    def fail_settlement(db, kind, *args, **kwargs):
        if kind == 'run.succeeded':
            raise RuntimeError('Simulated interrupted settlement')
        return original(db, kind, *args, **kwargs)
    monkeypatch.setattr(service, '_event', fail_settlement)
    with pytest.raises(RuntimeError):
        bridge.complete(run['id'], session)
    assert service.store.one('SELECT phase FROM desktop_runs WHERE run_id=?', (run['id'],))['phase'] == 'attached'
    assert service.get_task(task['id'])['runs'][0]['status'] == 'running'
    monkeypatch.setattr(service, '_event', original)
    bridge.complete(run['id'], session)
    assert service.get_task(task['id'])['runs'][0]['status'] == 'succeeded'


async def test_restart_preserves_exhausted_delivery_budget_and_future_deadline(service, tmp_path):
    bridge, task, run = dispatched(service, tmp_path)
    fake = tmp_path / 'codex'; count = tmp_path / 'attempts'
    fake.write_text(f'#!/bin/sh\necho attempt >> "{count}"\nexit 1\n'); fake.chmod(0o755)
    service.settings.codex_command = str(fake)
    with service.store.transaction() as db:
        db.execute("UPDATE desktop_runs SET delivery_attempts=3,queue_error='Exhausted' WHERE run_id=?", (run['id'],))
    worker = Worker(service); await worker.start()
    try:
        await asyncio.sleep(.15)
        assert not count.exists()
        assert service.store.one('SELECT delivery_attempts FROM desktop_runs WHERE run_id=?',(run['id'],))['delivery_attempts'] == 3
    finally:
        await worker.stop()
    import time
    deadline = time.time() + 90
    with service.store.transaction() as db:
        db.execute('UPDATE desktop_runs SET delivery_attempts=1,next_attempt_at=? WHERE run_id=?', (deadline,run['id']))
    worker = Worker(service); await worker.start()
    try:
        await asyncio.sleep(.15)
        assert not count.exists()
        assert service.store.one('SELECT next_attempt_at FROM desktop_runs WHERE run_id=?',(run['id'],))['next_attempt_at'] == deadline
    finally:
        await worker.stop()


@pytest.mark.parametrize('result', ['input', 'review'])
async def test_acknowledgement_delay_preserves_submitted_result_and_followup(service, tmp_path, monkeypatch, result):
    import time
    bridge, task, run = dispatched(service, tmp_path)
    bridge.claim(run['id']); session = str(uuid4()); bridge.started(run['id'], session)
    bridge.action(run['id'], session, result, text='Completed turn')
    if result == 'review':
        followup = service.request_changes(task['id'], 'Approved follow-up')['run_id']
    else:
        blocker = service.get_task(task['id'])['blocker_comment_id']
        service.add_comment(task['id'], 'Approved follow-up', resume=True, in_reply_to=blocker)
        followup = service.store.one("SELECT id FROM runs WHERE status='queued'")['id']
    with service.store.transaction() as db:
        db.execute('UPDATE runs SET started_at=?,activity_finished_at=? WHERE id=?',
                   (time.time() - service.settings.run_timeout_seconds - 10, time.time() - 130, run['id']))
    async def queue(*args): return {'queued': True}
    monkeypatch.setattr(DesktopBridge, 'queue', queue)
    worker = Worker(service)
    pending = asyncio.create_task(worker.run_once(run))
    try:
        await asyncio.sleep(.08)
        current = service.get_task(task['id'])
        assert next(r for r in current['runs'] if r['id'] == run['id'])['status'] == 'running'
        assert next(r for r in current['runs'] if r['id'] == followup)['status'] == 'queued'
        assert bridge.status()['state'] == 'awaiting_acknowledgment'
        assert bridge.status()['requires_attention']
        assert len([c for c in current['comments'] if 'Awaiting dispatcher acknowledgment' in c['body']]) == 1
        bridge.complete(run['id'], session)
        await pending
        assert service.claim_next()['id'] == followup
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


async def test_result_committed_during_delivery_retry_wins_over_timeout(service, tmp_path, monkeypatch):
    import time
    bridge, task, run = dispatched(service, tmp_path)
    bridge.claim(run['id']); session = str(uuid4()); bridge.started(run['id'], session)
    with service.store.transaction() as db:
        db.execute('UPDATE runs SET started_at=? WHERE id=?', (time.time() - service.settings.run_timeout_seconds - 10, run['id']))
        db.execute("UPDATE desktop_runs SET queue_error='transient',next_attempt_at=0 WHERE run_id=?", (run['id'],))
    async def queue(*args):
        bridge.action(run['id'], session, 'input', text='Approve A?')
        blocker = service.get_task(task['id'])['blocker_comment_id']
        service.add_comment(task['id'], 'Approved A', resume=True, in_reply_to=blocker)
        with service.store.transaction() as db:
            db.execute('UPDATE desktop_runs SET next_attempt_at=NULL WHERE run_id=?', (run['id'],))
        return {'queued': True}
    monkeypatch.setattr(DesktopBridge, 'queue', queue)
    pending = asyncio.create_task(Worker(service).run_once(run))
    try:
        await asyncio.sleep(.08)
        current = service.get_task(task['id'])
        assert {r['status'] for r in current['runs']} == {'running', 'queued'}
        bridge.complete(run['id'], session)
        await pending
    finally:
        pending.cancel(); await asyncio.gather(pending, return_exceptions=True)
