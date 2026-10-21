import httpx
import pytest

from acp.integrations import Integrations
from acp.service import DomainError
from conftest import reviewed, started
from test_integrations import comment_payload, imported


def attached(service):
    """A Codex app-server run with a live turn, as the runner leaves it after turn/start."""
    task, run = started(service, provider='codex')
    service.remember_session(run['id'], 'thread-1')
    service.mark_steerable(run['id'])
    return task, run


def test_human_comment_steers_live_turn_without_second_run(service):
    task, run = attached(service)
    comment = service.add_comment(task['id'], 'Use the smaller design')
    assert [c['id'] for c in service.pending_inputs(run['id'])] == [comment['id']]
    assert service.claim_next() is None
    service.input_delivered(comment['id'])
    assert service.pending_inputs(run['id']) == []
    service.submit_for_review(task['id'], 'Applied the feedback', run_id=run['id'])
    service.finish_run(run['id'])
    assert service.claim_next() is None
    assert service.get_task(task['id'])['runs'][0]['status'] == 'succeeded'


def test_pending_steering_prevents_stale_pause(service):
    task, run = attached(service)
    service.add_comment(task['id'], 'One more requirement')
    with pytest.raises(DomainError, match='comments'):
        service.submit_for_review(task['id'], 'Stale result', run_id=run['id'])
    with pytest.raises(DomainError, match='comments'):
        service.request_input(task['id'], 'Stale question', run['id'])


def test_agent_comments_and_edits_do_not_steer(service):
    task, run = attached(service)
    comment = service.add_comment(task['id'], 'Progress', run_id=run['id'])
    service.edit_comment(task['id'], comment['id'], 'Revised progress')
    assert service.pending_inputs(run['id']) == []


def test_undelivered_steering_becomes_a_followup(service):
    task, run = attached(service)
    comment = service.add_comment(task['id'], 'Also handle errors')
    with service.store.transaction() as db:  # The turn ended before the steer landed.
        db.execute("UPDATE tasks SET state='ready_for_review' WHERE id=?", (task['id'],))
    service.finish_run(run['id'])
    followup = service.claim_next()
    assert followup['inputs'] == [{'trigger': 'comment', 'comment_id': comment['id']}]
    assert service.get_task(task['id'])['session_id'] == 'thread-1'


def test_comment_on_review_resumes_same_thread(service):
    task, run = attached(service)
    service.submit_for_review(task['id'], 'Ready', run_id=run['id'])
    comment = service.add_comment(task['id'], 'Please simplify this')
    assert service.claim_next() is None
    service.finish_run(run['id'])
    followup = service.claim_next()
    assert followup['inputs'] == [{'trigger': 'comment', 'comment_id': comment['id']}]
    assert service.get_task(task['id'])['session_id'] == 'thread-1'


def test_cli_comment_schedules_retained_session_followup(service):
    task, run = started(service, provider='claude')
    service.remember_session(run['id'], 'retained-cli')
    comment = service.add_comment(task['id'], 'Also check this edge case')
    assert service.claim_next() is None
    service.submit_for_review(task['id'], 'Done', run_id=run['id'])
    service.finish_run(run['id'])
    followup = service.claim_next()
    assert followup['inputs'] == [{'trigger': 'comment', 'comment_id': comment['id']}]
    assert service.get_task(task['id'])['session_id'] == 'retained-cli'


@pytest.mark.parametrize('state', ['planning', 'deploy', 'done'])
def test_comments_in_inactive_lanes_do_not_launch_work(service, state):
    if state == 'planning':
        task = service.create_task('Unscheduled')
    else:
        task, _ = reviewed(service)
        service.move_task(task['id'], state)
    service.add_comment(task['id'], 'Just a note')
    assert service.claim_next() is None


async def test_linear_human_comment_steers_once_and_filters_bots_edits_mirrors(linear_service):
    task = await imported(linear_service)
    linear_service.update_task(task['id'], provider='codex')
    linear_service.move_task(task['id'], 'todo')
    run = linear_service.claim_next()
    linear_service.mark_started(run['id'])
    linear_service.mark_steerable(run['id'])
    async with httpx.AsyncClient() as client:
        integration = Integrations(linear_service, client)
        payload = comment_payload('Adjust the design')
        await integration.handle_linear(payload)
        await integration.handle_linear(payload)
        [pending] = linear_service.pending_inputs(run['id'])
        assert 'Adjust the design' in pending['body']
        linear_service.input_delivered(pending['id'])
        payload['action'] = 'update'
        payload['data']['body'] = 'Edited later'
        await integration.handle_linear(payload)
        bot = comment_payload('Bot note', 'bot')
        bot['actor']['type'] = 'application'
        await integration.handle_linear(bot)
        await integration.handle_linear(comment_payload(f'<!-- acp:{pending["id"]} -->\nMirrored text', 'mirror'))
    assert linear_service.pending_inputs(run['id']) == []
    assert len(linear_service.get_task(task['id'])['runs']) == 1


def test_deleted_pending_comment_does_not_steer_or_block_review(service):
    task, run = attached(service)
    comment = service.add_comment(task['id'], 'Withdraw this feedback')
    service.edit_comment(task['id'], comment['id'], delete=True)
    assert service.pending_inputs(run['id']) == []
    service.submit_for_review(task['id'], 'Ready', run_id=run['id'])


def test_human_pause_does_not_replay_pending_comments(service):
    task, run = attached(service)
    service.add_comment(task['id'], 'Additional requirement')
    service.request_input(task['id'], 'Human paused work')
    service.finish_run(run['id'])
    assert service.claim_next() is None
    assert service.pending_inputs(run['id']) == []
