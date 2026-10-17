import json
from concurrent.futures import ThreadPoolExecutor

import pytest

from acp.config import STATES
from acp.service import DomainError
from conftest import reviewed, started


def test_exactly_seven_lanes(client):
    config = client.get('/api/config').json()
    assert [s['id'] for s in config['states']] == list(STATES)
    assert config['default_provider'] == 'demo'
    assert client.get('/').status_code == 200
    assert client.get('/static/app.js').status_code == 200


def test_todo_queues_but_is_not_yet_in_progress(service):
    task = service.create_task('Task', state='todo')
    assert task['state'] == 'todo'
    run = service.claim_next()
    assert service.get_task(task['id'])['state'] == 'todo'
    service.mark_started(run['id'])
    assert service.get_task(task['id'])['state'] == 'in_progress'
    assert len(service.store.events()) == 5


@pytest.mark.parametrize('question', ['', ' ', '\n\t'])
def test_waiting_requires_nonempty_comment_atomic(service, question):
    task, run = started(service)
    before = service.store.events()
    with pytest.raises(DomainError):
        service.request_input(task['id'], question, run['id'])
    after = service.get_task(task['id'])
    assert after['state'] == 'in_progress'
    assert after['comments'] == []
    assert service.store.events() == before


def test_question_and_transition_share_transaction(service):
    task, run = started(service)
    service.request_input(task['id'], 'Which behavior?', run['id'])
    task = service.get_task(task['id'])
    assert task['state'] == 'waiting_on_input'
    assert task['blocker_comment_id'] == task['comments'][0]['id']
    events = service.store.events()
    assert events[-2]['type'] == 'comment.created'
    assert events[-1]['type'] == 'task.state_changed'
    assert events[-1]['data']['comment_id'] == task['blocker_comment_id']


def test_outbox_failure_rolls_back_state_and_comment(service, monkeypatch):
    task, run = started(service)
    original = service._event
    def fail(db, kind, *args, **kwargs):
        if kind == 'task.state_changed':
            raise RuntimeError('simulated write failure')
        return original(db, kind, *args, **kwargs)
    monkeypatch.setattr(service, '_event', fail)
    with pytest.raises(RuntimeError):
        service.request_input(task['id'], 'Question?', run['id'])
    task = service.get_task(task['id'])
    assert task['state'] == 'in_progress' and not task['comments']


def test_idempotent_state_move_does_not_duplicate_runs(service):
    task = service.create_task('Concurrent task')
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: service.move_task(task['id'], 'todo'), range(16)))
    assert all(r['state'] == 'todo' for r in results)
    assert len(service.get_task(task['id'])['runs']) == 1
    assert len([e for e in service.store.events() if e['type'] == 'task.state_changed']) == 1


def test_stale_expected_version_rejected(service):
    task = service.create_task('Task')
    service.update_task(task['id'], title='Changed')
    with pytest.raises(DomainError, match='Refresh'):
        service.move_task(task['id'], 'todo', expected_version=task['version'])


def test_in_progress_owned_by_runner(service):
    task = service.create_task('Task', state='todo')
    with pytest.raises(DomainError, match='runner'):
        service.move_task(task['id'], 'in_progress')


def test_comments_publish_create_edit_delete_events(service):
    task = service.create_task('Task')
    comment = service.add_comment(task['id'], 'First')
    service.edit_comment(task['id'], comment['id'], 'Second')
    service.edit_comment(task['id'], comment['id'], delete=True)
    assert service.get_task(task['id'])['comments'] == []
    assert [e['type'] for e in service.store.events()][-3:] == ['comment.created','comment.updated','comment.deleted']


def test_current_blocker_cannot_be_deleted(service):
    task, run = started(service)
    service.request_input(task['id'], 'Why?', run['id'])
    task = service.get_task(task['id'])
    with pytest.raises(DomainError, match='cannot be deleted'):
        service.edit_comment(task['id'], task['blocker_comment_id'], delete=True)


def test_agent_comments_never_schedule_a_run(service):
    task, run = started(service)
    service.add_comment(task['id'], 'Plan', run_id=run['id'])
    assert len(service.get_task(task['id'])['runs']) == 1
    with pytest.raises(DomainError, match='cannot resume themselves'):
        service.add_comment(task['id'], 'Again', run_id=run['id'], resume=True)


def test_stale_run_cannot_modify_task(service):
    task, run = started(service)
    service.cancel(task['id'])
    with pytest.raises(DomainError, match='no longer active'):
        service.add_comment(task['id'], 'Late callback', run_id=run['id'])
    with pytest.raises(DomainError):
        service.submit_for_review(task['id'], 'Late output', run_id=run['id'])


def test_explicit_answer_queues_one_followup_and_waits_for_current_exit(service):
    task, run = started(service)
    service.remember_session(run['id'], 'session-123')
    service.request_input(task['id'], 'Question?', run['id'])
    task = service.get_task(task['id'])
    service.add_comment(task['id'], 'Answer', in_reply_to=task['blocker_comment_id'], resume=True)
    service.add_comment(task['id'], 'Additional answer', in_reply_to=task['blocker_comment_id'], resume=True)
    assert service.claim_next() is None
    service.finish_run(run['id'])
    next_run = service.claim_next()
    assert len(next_run['inputs']) == 2
    assert service.get_task(task['id'])['session_id'] == 'session-123'
    service.mark_started(next_run['id'])
    assert service.get_task(task['id'])['state'] == 'in_progress'


def test_regular_comment_while_waiting_resumes_for_evaluation(service):
    task, run = started(service)
    service.request_input(task['id'], 'Question?', run['id'])
    service.finish_run(run['id'])
    comment = service.add_comment(task['id'], 'Additional context')
    followup = service.claim_next()
    assert followup['inputs'] == [{'trigger': 'input', 'comment_id': comment['id']}]
    service.mark_started(followup['id'])
    assert service.get_task(task['id'])['state'] == 'in_progress'


def test_reply_to_older_comment_still_resumes_waiting_task(service):
    task, run = started(service)
    c = service.add_comment(task['id'], 'Plan', run_id=run['id'])
    service.request_input(task['id'], 'Current question?', run['id'])
    answer = service.add_comment(task['id'], 'More context', in_reply_to=c['id'], resume=True)
    service.finish_run(run['id'])
    assert service.claim_next()['inputs'] == [{'trigger': 'input', 'comment_id': answer['id']}]


def test_wrong_task_reply_rejected(service):
    a = service.create_task('A')
    b = service.create_task('B')
    c = service.add_comment(a['id'], 'Hi')
    with pytest.raises(DomainError):
        service.add_comment(b['id'], 'Wrong parent', in_reply_to=c['id'])


def test_review_loop_keeps_artifact_and_session(service):
    task, run = started(service)
    service.remember_session(run['id'], 'same-session')
    service.submit_for_review(task['id'], 'v1', 'https://github.com/example/project/pull/7', 'pr', run['id'])
    service.finish_run(run['id'])
    assert service.claim_next() is None
    service.request_changes(task['id'], 'Fix error handling')
    followup = service.claim_next()
    service.mark_started(followup['id'])
    service.submit_for_review(task['id'], 'v2', 'https://github.com/example/project/pull/7', 'pr', followup['id'])
    service.finish_run(followup['id'])
    updated = service.get_task(task['id'])
    assert updated['session_id'] == 'same-session'
    assert len(updated['artifacts']) == 1 and updated['artifacts'][0]['summary'] == 'v2'


def test_move_away_cancels_queue_and_live_run_before_requeue(service):
    task, run = started(service)
    service.move_task(task['id'], 'todo')
    assert service.claim_next() is None
    service.finish_run(run['id'])
    assert service.get_task(task['id'])['runs'][-1]['status'] == 'cancelled'
    assert service.claim_next() is not None


def test_worker_exit_without_terminal_tool_is_failure(service):
    task, run = started(service)
    service.finish_run(run['id'])
    task = service.get_task(task['id'])
    assert task['state'] == 'waiting_on_input'
    assert task['runs'][0]['status'] == 'failed'
    assert task['blocker_comment_id']
    assert service.claim_next() is None


def test_restart_recovers_running_not_queued(service):
    task, run = started(service)
    queued = service.create_task('Queued', state='todo')
    service.recover()
    task = service.get_task(task['id'])
    assert task['state'] == 'waiting_on_input'
    assert task['runs'][0]['status'] == 'interrupted'
    assert service.get_task(queued['id'])['runs'][0]['status'] == 'queued'


def test_change_provider_requires_idle_and_resets_session(service):
    task, run = reviewed(service)
    service.update_task(task['id'], provider='codex')
    assert service.get_task(task['id'])['session_id'] is None
    service.move_task(task['id'], 'todo')
    with pytest.raises(DomainError):
        service.update_task(task['id'], provider='claude')


def test_deploy_done_are_explicit(service):
    task, run = reviewed(service)
    service.move_task(task['id'], 'deploy')
    assert service.claim_next() is None
    service.move_task(task['id'], 'done')
    service.move_task(task['id'], 'todo')
    assert service.claim_next() is not None


@pytest.mark.parametrize('url', ['javascript:alert(1)', 'https://evil.com/a/b/pull/1', 'https://github.com/a/b/issues/2'])
def test_pr_urls_validated(service, url):
    task, run = started(service)
    with pytest.raises(DomainError):
        service.submit_for_review(task['id'], 'Summary', url, 'pr', run['id'])


def test_api_payload_and_origin_checks(client):
    assert client.post('/api/tasks', json={'title':'', 'provider':'other'}).status_code == 422
    assert client.post('/api/tasks', json={'title':'T', 'unknown':'ignored?'}).status_code == 422
    assert client.post('/api/tasks', json={'title':'T'}, headers={'Origin':'https://untrusted.example'}).status_code == 403
    assert client.get('/api/tasks', headers={'Host':'untrusted.example'}).status_code == 400


def test_event_replay_and_task_filter(service):
    a = service.create_task('A')
    b = service.create_task('B')
    service.add_comment(a['id'], 'C')
    page = service.store.events(after=1, task_id=a['id'], limit=1)
    assert len(page) == 1 and page[0]['type'] == 'comment.created'
    assert page[0]['schema_version'] == 1
