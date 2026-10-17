from uuid import uuid4

import httpx
import pytest

from acp.desktop import DesktopBridge
from acp.integrations import Integrations
from acp.service import DomainError
from conftest import reviewed, started
from test_integrations import comment_payload, imported


def attached(service):
    bridge = DesktopBridge(service)
    bridge.connect(str(uuid4()))
    task = service.create_task('Steer this task', provider='codex', state='todo')
    run = service.claim_next()
    bridge.dispatch(run['id'])
    bridge.claim(run['id'])
    session = str(uuid4())
    bridge.started(run['id'], session)
    return bridge, task, run, session


def test_human_comment_steers_bound_chat_without_second_run(service):
    bridge, task, run, session = attached(service)
    comment = service.add_comment(task['id'], 'Use the smaller design')
    delivery = bridge.steering(run['id'])
    assert delivery['action'] == 'send'
    assert delivery['session_id'] == session
    assert delivery['comment_id'] == comment['id']
    assert 'Use the smaller design' in delivery['prompt']
    assert len(service.get_task(task['id'])['runs']) == 1
    assert bridge.steering(run['id'])['action'] == 'uncertain'
    bridge.steered(run['id'], session, comment['id'])
    bridge.steered(run['id'], session, comment['id'])
    assert bridge.steering(run['id'])['action'] == 'wait'
    bridge.action(run['id'], session, 'review', text='Applied the feedback')
    bridge.complete(run['id'], session)
    assert service.get_task(task['id'])['runs'][0]['status'] == 'succeeded'


def test_pending_steering_prevents_stale_pause_and_wrong_ack(service):
    bridge, task, run, session = attached(service)
    comment = service.add_comment(task['id'], 'One more requirement')
    for action in ('review', 'input'):
        with pytest.raises(DomainError, match='comments'):
            bridge.action(run['id'], session, action, text='Stale result')
    with pytest.raises(DomainError):
        bridge.steered(run['id'], session, comment['id'])
    bridge.steering(run['id'])
    with pytest.raises(DomainError):
        bridge.steered(run['id'], str(uuid4()), comment['id'])
    service.cancel(task['id'])
    with pytest.raises(DomainError):
        bridge.steering(run['id'])
    with pytest.raises(DomainError):
        bridge.steered(run['id'], session, comment['id'])
    bridge.complete(run['id'], session)
    assert service.claim_next() is None


def test_agent_comments_and_edits_do_not_steer(service):
    bridge, task, run, session = attached(service)
    comment = bridge.action(run['id'], session, 'comment', text='Progress')
    service.edit_comment(task['id'], comment['id'], 'Revised progress')
    assert bridge.steering(run['id'])['action'] == 'wait'


def test_comment_on_review_automatically_resumes_same_chat(service):
    bridge, task, run, session = attached(service)
    bridge.action(run['id'], session, 'review', text='Ready')
    comment = service.add_comment(task['id'], 'Please simplify this')
    assert service.claim_next() is None
    bridge.complete(run['id'], session)
    followup = service.claim_next()
    assert followup['inputs'] == [{'trigger': 'comment', 'comment_id': comment['id']}]
    bridge.dispatch(followup['id'])
    assert bridge.claim(followup['id'])['session_id'] == session


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
    bridge = DesktopBridge(linear_service)
    bridge.connect(str(uuid4()))
    run = linear_service.claim_next()
    bridge.dispatch(run['id'])
    bridge.claim(run['id'])
    session = str(uuid4())
    bridge.started(run['id'], session)
    async with httpx.AsyncClient() as client:
        integration = Integrations(linear_service, client)
        payload = comment_payload('Adjust the design')
        await integration.handle_linear(payload)
        await integration.handle_linear(payload)
        delivery = bridge.steering(run['id'])
        assert delivery['action'] == 'send'
        assert 'Adjust the design' in delivery['prompt']
        bridge.steered(run['id'], session, delivery['comment_id'])
        payload['action'] = 'update'
        payload['data']['body'] = 'Edited later'
        await integration.handle_linear(payload)
        bot = comment_payload('Bot note', 'bot')
        bot['actor']['type'] = 'application'
        await integration.handle_linear(bot)
        await integration.handle_linear(comment_payload(
            f'<!-- acp:{delivery["comment_id"]} -->\nMirrored text', 'mirror'))
    assert bridge.steering(run['id'])['action'] == 'wait'
    assert len(linear_service.get_task(task['id'])['runs']) == 1


def test_deleted_pending_comment_does_not_steer_or_block_review(service):
    bridge, task, run, session = attached(service)
    comment = service.add_comment(task['id'], 'Withdraw this feedback')
    service.edit_comment(task['id'], comment['id'], delete=True)
    assert bridge.steering(run['id'])['action'] == 'wait'
    bridge.action(run['id'], session, 'review', text='Ready')


def test_unresolved_send_cannot_be_completed_as_success(service):
    bridge, task, run, session = attached(service)
    service.add_comment(task['id'], 'New requirement')
    bridge.steering(run['id'])
    with pytest.raises(DomainError, match='comments'):
        bridge.complete(run['id'], session)
    assert service.get_task(task['id'])['runs'][0]['status'] == 'running'
    # Explicit failures can still settle the run and retain feedback for a human retry.
    bridge.complete(run['id'], session, error='Unable to reconcile delivery')
    assert service.get_task(task['id'])['runs'][0]['status'] == 'failed'


@pytest.mark.parametrize('claimed', [False, True])
def test_human_pause_revokes_steering_without_replaying_comments(service, claimed):
    bridge, task, run, session = attached(service)
    comment = service.add_comment(task['id'], 'Additional requirement')
    if claimed:
        bridge.steering(run['id'])
    service.request_input(task['id'], 'Human paused work')
    with pytest.raises(DomainError):
        bridge.steering(run['id'])
    with pytest.raises(DomainError):
        bridge.steered(run['id'], session, comment['id'])
    bridge.complete(run['id'], session)
    assert service.claim_next() is None


def test_steering_api_roundtrip_and_wrong_chat(client):
    service = client.app.state.service
    bridge, task, run, session = attached(service)
    comment = service.add_comment(task['id'], 'API feedback')
    path = f'/api/desktop/runs/{run["id"]}'
    result = client.post(path + '/steering', json={})
    assert result.status_code == 200 and result.json()['session_id'] == session
    assert client.post(path + '/steered', json={
        'thread_id': str(uuid4()), 'comment_id': comment['id']}).status_code == 409
    assert client.post(path + '/steered', json={
        'thread_id': session, 'comment_id': comment['id']}).json() == {'delivered': True}
