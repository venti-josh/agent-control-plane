from uuid import uuid4

import pytest

from acp.service import DomainError


def desktop(service):
    from acp.desktop import DesktopBridge
    service.settings.repo_search_roots = []
    return DesktopBridge(service)


def run(service):
    task = service.create_task('App task', provider='codex', state='todo')
    return task, service.claim_next()


def test_connection_validates_uuid_and_refuses_active_replacement(service):
    bridge = desktop(service)
    with pytest.raises(DomainError):
        bridge.connect('not-a-thread')
    thread = str(uuid4())
    assert bridge.connect(thread)['thread_id'] == thread
    task, job = run(service)
    bridge.dispatch(job['id'])
    with pytest.raises(DomainError):
        bridge.connect(str(uuid4()))


def test_claim_is_once_and_existing_cli_session_is_not_adopted(service):
    bridge = desktop(service)
    bridge.connect(str(uuid4()))
    task, job = run(service)
    service.remember_session(job['id'], str(uuid4()))
    bridge.dispatch(job['id'])
    first = bridge.claim(job['id'])
    assert first['action'] == 'create'
    assert first['session_id'] is None
    assert bridge.claim(job['id'])['action'] == 'uncertain'
    assert service.get_task(task['id'])['state'] == 'todo'


def test_started_binds_session_actions_and_completion(service):
    bridge = desktop(service)
    bridge.connect(str(uuid4()))
    task, job = run(service)
    bridge.dispatch(job['id'])
    bridge.claim(job['id'])
    thread = str(uuid4())
    bridge.started(job['id'], thread)
    bridge.started(job['id'], thread)
    assert service.get_task(task['id'])['state'] == 'in_progress'
    with pytest.raises(DomainError):
        bridge.started(job['id'], str(uuid4()))
    bridge.action(job['id'], thread, 'review', text='Tested')
    assert service.store.one('SELECT status FROM runs WHERE id=?', (job['id'],))['status'] == 'running'
    bridge.complete(job['id'], thread)
    bridge.complete(job['id'], thread)
    assert service.get_task(task['id'])['runs'][0]['status'] == 'succeeded'
    with pytest.raises(DomainError):
        bridge.action(job['id'], thread, 'comment', text='late')
    service.request_changes(task['id'], 'revise')
    next_run = service.claim_next()
    bridge.dispatch(next_run['id'])
    assert bridge.claim(next_run['id'])['session_id'] == thread


def test_cancel_blocks_late_results_and_new_dispatch_until_app_completes(service):
    bridge = desktop(service)
    bridge.connect(str(uuid4()))
    task, job = run(service)
    bridge.dispatch(job['id'])
    bridge.claim(job['id'])
    thread = str(uuid4())
    bridge.started(job['id'], thread)
    service.cancel(task['id'])
    service.finish_run(job['id'])
    with pytest.raises(DomainError):
        bridge.action(job['id'], thread, 'review', text='late result')
    with pytest.raises(DomainError):
        service.reset_session(task['id'])
    service.move_task(task['id'], 'todo')
    following = service.claim_next()
    with pytest.raises(DomainError, match='still'):
        bridge.dispatch(following['id'])
    bridge.complete(job['id'], thread)
    assert bridge.dispatch(following['id'])


def test_desktop_api_is_local_only(client, settings):
    response = client.post('/api/desktop/connection', json={'thread_id': str(uuid4())})
    assert response.status_code == 200
    assert client.get('/api/desktop').json()['connected']
    from acp.api import create_app
    from fastapi.testclient import TestClient
    with TestClient(create_app(settings, start_worker=False, hooks_only=True)) as hooks:
        assert hooks.get('/api/desktop').status_code == 404


def test_disconnect_and_provider_switch_cannot_abandon_unsettled_chat(service):
    bridge = desktop(service)
    bridge.connect(str(uuid4()))
    task, job = run(service)
    bridge.dispatch(job['id'])
    bridge.claim(job['id'])
    bridge.started(job['id'], str(uuid4()))
    service.cancel(task['id'])
    service.finish_run(job['id'])
    with pytest.raises(DomainError):
        bridge.connect(None)
    with pytest.raises(DomainError):
        service.update_task(task['id'], provider='demo')


@pytest.mark.asyncio
async def test_queue_ack_does_not_mark_started_or_finish_and_cancel_revokes(service, tmp_path):
    import asyncio
    from acp.desktop import DesktopRunner
    fake = tmp_path / 'codex'
    fake.write_text('#!/bin/sh\nprintf "Queued test\\n"\n')
    fake.chmod(0o755)
    service.settings.codex_command = str(fake)
    bridge = desktop(service)
    bridge.connect(str(uuid4()))
    task, job = run(service)
    pending = asyncio.create_task(DesktopRunner().run(service, task, job))
    try:
        for _ in range(100):
            if (service.settings.data_dir / 'logs' / (job['id'] + '.jsonl')).exists():
                break
            await asyncio.sleep(.01)
        assert service.get_task(task['id'])['state'] == 'todo'
        assert not pending.done()
        service.cancel(task['id'])
        await asyncio.wait_for(pending, 2)
        with pytest.raises(DomainError):
            bridge.claim(job['id'])
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_disconnected_app_session_never_falls_back_to_cli(service):
    from acp.desktop import DesktopRunner
    bridge = desktop(service)
    bridge.connect(str(uuid4()))
    task, job = run(service)
    bridge.dispatch(job['id'])
    bridge.claim(job['id'])
    thread = str(uuid4())
    bridge.started(job['id'], thread)
    bridge.action(job['id'], thread, 'review', text='done')
    bridge.complete(job['id'], thread)
    bridge.connect(None)
    service.request_changes(task['id'], 'revise')
    following = service.claim_next()
    with pytest.raises(DomainError, match='Reconnect'):
        await DesktopRunner().run(service, service.get_task(task['id']), following)


@pytest.mark.parametrize('recover', [False, True])
def test_reconcile_chat_created_just_before_cancel_or_restart(service, recover):
    bridge = desktop(service)
    bridge.connect(str(uuid4()))
    task, job = run(service)
    bridge.dispatch(job['id'])
    bridge.claim(job['id'])
    created_chat = str(uuid4())
    if recover:
        service.recover()
    else:
        service.cancel(task['id'])
        service.finish_run(job['id'])
    state = service.get_task(task['id'])['state']
    bridge.bind(job['id'], created_chat)
    bridge.complete(job['id'], created_chat)
    assert service.get_task(task['id'])['state'] == state
    assert service.get_task(task['id'])['session_id'] is None
    with pytest.raises(DomainError):
        bridge.started(job['id'], created_chat)
    bridge.connect(None)


def test_resumed_prepare_failure_can_be_acknowledged_without_new_chat(service):
    bridge = desktop(service)
    bridge.connect(str(uuid4()))
    task, job = run(service)
    bridge.dispatch(job['id'])
    bridge.claim(job['id'])
    session = str(uuid4())
    bridge.started(job['id'], session)
    bridge.action(job['id'], session, 'review', text='done')
    bridge.complete(job['id'], session)
    service.request_changes(task['id'], 'revise')
    following = service.claim_next()
    bridge.dispatch(following['id'])
    assert bridge.claim(following['id'])['action'] == 'resume'
    bridge.complete(following['id'], '', 'Retained worktree is missing; no prompt sent')
    assert service.get_task(task['id'])['runs'][0]['status'] == 'failed'
    bridge.connect(None)


def test_claim_rechecks_cancellation_inside_transaction(service, monkeypatch):
    bridge = desktop(service)
    bridge.connect(str(uuid4()))
    task, job = run(service)
    bridge.dispatch(job['id'])
    original = bridge.active
    def cancel_between_read_and_transaction(run_id):
        result = original(run_id)
        service.cancel(task['id'])
        return result
    monkeypatch.setattr(bridge, 'active', cancel_between_read_and_transaction)
    with pytest.raises(DomainError):
        bridge.claim(job['id'])
    assert service.store.one('SELECT phase FROM desktop_runs WHERE run_id=?', (job['id'],))['phase'] == 'dispatched'


def test_acknowledge_paused_turn_does_not_complete_task(client):
    service = client.app.state.service
    bridge = desktop(service)
    bridge.connect(str(uuid4()))
    task, job = run(service)
    bridge.dispatch(job['id'])
    bridge.claim(job['id'])
    session = str(uuid4())
    bridge.started(job['id'], session)
    bridge.action(job['id'], session, 'input', text='Which name?')
    result = client.post(f'/api/desktop/runs/{job["id"]}/acknowledge', json={'thread_id': session})
    assert result.status_code == 200
    task = service.get_task(task['id'])
    assert task['state'] == 'waiting_on_input'
    assert task['runs'][0]['status'] == 'succeeded'


@pytest.mark.asyncio
async def test_app_closed_start_timeout_never_reports_execution(service, tmp_path, monkeypatch):
    from acp.desktop import DesktopRunner
    import acp.desktop as module
    fake = tmp_path / 'codex'
    fake.write_text('#!/bin/sh\nprintf "Queued test\\n"\n')
    fake.chmod(0o755)
    service.settings.codex_command = str(fake)
    service.settings.run_timeout_seconds = 1800
    bridge = desktop(service)
    bridge.connect(str(uuid4()))
    task, job = run(service)
    # Give the runner its own clock, without changing asyncio's clock.
    from types import SimpleNamespace
    times = iter([0, 121, 1801])
    monkeypatch.setattr(module, 'time', SimpleNamespace(monotonic=lambda: next(times)))
    with pytest.raises(TimeoutError, match='did not start'):
        await DesktopRunner().run(service, task, job)
    assert service.get_task(task['id'])['state'] == 'todo'
    assert service.get_task(task['id'])['runs'][0]['started_at'] is None


def test_dispatcher_serializes_codex_runs_but_other_providers_can_run(service):
    bridge = desktop(service)
    bridge.connect(str(uuid4()))
    first, job = run(service)
    second = service.create_task('Another app task', provider='codex', state='todo')
    demo = service.create_task('Independent demo', provider='demo', state='todo')
    assert service.claim_next()['task_id'] == demo['id']
    assert service.claim_next() is None
    service.finish_run(job['id'], 'failed', 'Test ended')
    assert service.claim_next()['task_id'] == second['id']


@pytest.mark.asyncio
async def test_prepare_delegates_workspace_without_touching_repository(service, tmp_path):
    import subprocess
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', '-b', 'main', str(repo)], check=True, capture_output=True)
    bridge = desktop(service)
    task = service.create_task('Initialize project', repo_path=str(repo), provider='codex', state='todo')
    job = service.claim_next()
    prepared = await bridge.prepare(job['id'])
    assert prepared['repo_path'] == str(repo)
    task = service.get_task(task['id'])
    assert task['workspace'] is None and task['branch'] is None
    assert not (service.settings.data_dir / 'worktrees').exists()
    assert subprocess.check_output(['git', 'branch', '--list'], cwd=repo) == b''
