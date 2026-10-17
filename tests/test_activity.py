import pytest

from acp.store import Store
from acp.activity import Activity
from conftest import started


def collab(states, tool='spawn_agent', status='completed'):
    return {'type': 'item.completed', 'item': {
        'type': 'collab_tool_call', 'tool': tool, 'status': status,
        'agents_states': {key: {'status': value} for key, value in states.items()},
        'receiver_thread_ids': list(states), 'sender_thread_id': 'parent',
    }}


def test_activity_excludes_queued_and_claimed_but_unlaunched_runs(client):
    svc = client.app.state.service
    svc.create_task('Queued', state='todo')
    svc.claim_next()
    assert Activity(client.app.state.service.store).summary() == {
        'running': 0, 'subagents': 0, 'waiting_on_input': 0,
        'averages': {'30m': 0, '1h': 0, '3h': 0},
    }


def test_time_weighted_windows_include_finished_and_overlapping_runs(service, monkeypatch):
    from acp.activity import Activity
    monkeypatch.setattr('time.time', lambda: 20000)
    _, first = started(service)
    _, second = started(service)
    with service.store.transaction() as db:
        db.execute('UPDATE runs SET started_at=16400 WHERE id=?', (first['id'],))
        db.execute("UPDATE runs SET started_at=18200,finished_at=19100,status='succeeded' WHERE id=?",
                   (second['id'],))
    data = Activity(service.store).summary()
    assert data['running'] == 1
    assert data['averages'] == {'30m': 1.5, '1h': 1.25, '3h': pytest.approx(5 / 12)}


def test_subagents_deduplicate_resume_persist_and_stop_with_parent(service, monkeypatch):
    from acp.activity import Activity
    clock = [20000]
    monkeypatch.setattr('time.time', lambda: clock[0])
    _, run = started(service)
    activity = Activity(service.store)
    activity.observe(run['id'], 'codex', collab({'child': 'running'}))
    activity.observe(run['id'], 'codex', collab({'child': 'running'}, 'wait'))
    clock[0] += 900
    activity.observe(run['id'], 'codex', collab({'child': 'completed'}, 'wait'))
    assert activity.summary()['running'] == 1
    activity.observe(run['id'], 'codex', collab({'child': 'running'}, 'send_input'))
    clock[0] += 900
    assert Activity(Store(service.store.path)).summary()['averages']['30m'] == 2
    service.finish_run(run['id'], 'interrupted', 'Stopped')
    assert activity.summary()['running'] == 0
    assert activity.summary()['waiting_on_input'] == 1
    activity.observe(run['id'], 'codex', collab({'late': 'running'}))
    assert activity.summary()['running'] == 0
    clock[0] += 1800
    assert activity.summary()['averages']['30m'] == 0


def test_codex_calls_without_agent_states_and_non_agent_items_are_ignored(service):
    from acp.activity import Activity
    _, run = started(service)
    activity = Activity(service.store)
    activity.observe(run['id'], 'codex', {'type': 'item.completed',
                                       'item': {'type': 'collab_tool_call', 'status': 'failed'}})
    activity.observe(run['id'], 'codex', {'type': 'item.completed', 'item': {'type': 'command_execution'}})
    assert activity.summary()['subagents'] == 0


def test_claude_agent_lifecycle_does_not_count_background_shell_tasks(service):
    from acp.activity import Activity
    _, run = started(service)
    activity = Activity(service.store)
    def event(subtype, **fields):
        activity.observe(run['id'], 'claude', {'type': 'system', 'subtype': subtype, **fields})
    event('task_started', task_id='shell', task_type='local_bash')
    event('task_started', task_id='child', task_type='local_agent')
    event('task_started', task_id='child', task_type='local_agent')
    assert activity.summary()['subagents'] == 1
    event('task_updated', task_id='child', patch={'status': 'killed'})
    assert activity.summary()['subagents'] == 0
    event('task_started', task_id='child', task_type='local_agent')
    event('task_notification', task_id='child', status='completed')
    assert activity.summary()['subagents'] == 0


def test_recovery_closes_subagents_and_wait_count_survives_process_exit(service):
    from acp.activity import Activity
    task, run = started(service)
    activity = Activity(service.store)
    activity.observe(run['id'], 'codex', collab({'child': 'running'}))
    service.recover()
    assert activity.summary()['running'] == 0
    assert activity.summary()['waiting_on_input'] == 1
    service.add_comment(task['id'], 'Retry', in_reply_to=service.get_task(task['id'])['blocker_comment_id'],
                        resume=True)
    # The old input has been answered, even before the queued run launches.
    assert activity.summary()['waiting_on_input'] == 0


def test_desktop_reports_are_bound_validated_and_track_waiting_children(client, monkeypatch):
    """Wrong chat/late reports must never inflate concurrency or leave open intervals."""
    import json
    from uuid import uuid4
    from acp.desktop import DesktopBridge
    svc = client.app.state.service
    bridge = DesktopBridge(svc)
    task = svc.create_task('Desktop agents', provider='codex', state='todo')
    run = svc.claim_next()
    bridge.dispatch(run['id'])
    bridge.claim(run['id'])
    thread = str(uuid4())
    clock = [20000]
    monkeypatch.setattr('time.time', lambda: clock[0])
    bridge.started(run['id'], thread)
    url = f'/api/desktop/runs/{run["id"]}/action'
    def report(states, session=thread):
        return client.post(url, json={'thread_id': session, 'action': 'activity', 'text': json.dumps(states)})
    assert report({'child': 'running'}, str(uuid4())).status_code == 409
    assert report({'child': 'running'}).status_code == 200
    assert report({'child': 'running'}).status_code == 200
    clock[0] += 900
    assert report({'child': 'waiting_on_input'}).status_code == 200
    data = Activity(client.app.state.service.store).summary()
    assert data['running'] == 1
    assert data['waiting_on_input'] == 1
    assert report({'child': 'bogus'}).status_code == 422
    clock[0] += 900
    assert Activity(client.app.state.service.store).summary()['averages']['30m'] == 1.5
    assert report({'child': 'completed'}).status_code == 200
    assert Activity(client.app.state.service.store).summary()['waiting_on_input'] == 0
    bridge.action(run['id'], thread, 'input', text='Need direction')
    # A parent waiting for a person is no longer doing work, even before acknowledgement.
    assert Activity(client.app.state.service.store).summary()['running'] == 0
    assert Activity(client.app.state.service.store).summary()['waiting_on_input'] == 1
    assert report({'late': 'running'}).status_code == 409
    bridge.complete(run['id'], thread)
    assert report({'late': 'running'}).status_code == 409


def test_malformed_cli_events_cannot_break_tracking_or_count_parent_twice(service):
    from acp.activity import Activity
    _, run = started(service)
    service.remember_session(run['id'], 'parent')
    activity = Activity(service.store)
    for event in [ {'type': 'item.completed', 'item': ['malformed']},
                   {'type': 'item.completed', 'item': {'type': 'collab_tool_call', 'agents_states': []}},
                   collab({'parent': 'running', 'child': 'running'}) ]:
        activity.observe(run['id'], 'codex', event)
    assert activity.summary()['running'] == 2


def test_review_and_input_stop_work_history_before_run_acknowledgement(service, monkeypatch):
    from acp.activity import Activity
    clock = [20000]
    monkeypatch.setattr('time.time', lambda: clock[0])
    task, run = started(service)
    activity = Activity(service.store)
    activity.observe(run['id'], 'codex', collab({'child': 'running'}))
    clock[0] += 900
    service.submit_for_review(task['id'], 'Ready', run_id=run['id'])
    clock[0] += 900
    assert activity.summary()['running'] == 0
    assert activity.summary()['averages']['30m'] == 1
    service.finish_run(run['id'])
    clock[0] += 900
    assert activity.summary()['averages']['30m'] == 0


@pytest.mark.parametrize('terminal', ['errored', 'not_found'])
def test_failed_collaboration_call_still_closes_authoritative_terminal_state(service, terminal):
    from acp.activity import Activity
    _, run = started(service)
    activity = Activity(service.store)
    activity.observe(run['id'], 'codex', collab({'child': 'running'}))
    activity.observe(run['id'], 'codex', collab({'child': terminal}, tool='wait', status='failed'))
    assert activity.summary()['subagents'] == 0
