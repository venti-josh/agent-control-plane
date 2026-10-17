import hashlib
import hmac
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from acp.api import create_app
from acp.config import Settings
from acp.integrations import Integrations
from acp.service import DomainError
from conftest import reviewed, started


def issue(state='planning', updated='2026-10-16T12:00:00.000Z', **kwargs):
    return {'id':'issue-one', 'identifier':'ENG-1','url':'https://linear.app/team/issue/ENG-1',
            'title':'Linear task','description':'Implement the feature','updatedAt':updated,
            'state':{'id':f'state-{state}'},'team':{'id':'team-one'},**kwargs}


def gql_transport(remote):
    def respond(request):
        body = json.loads(request.content)
        assert request.headers['Authorization'] == 'test-key'
        if 'organization{id}' in body['query']:
            return httpx.Response(200,json={'data':{'organization':{'id':'workspace-one'},'issue':remote}})
        raise AssertionError(body)
    return httpx.MockTransport(respond)


async def imported(service, state='planning'):
    async with httpx.AsyncClient(transport=gql_transport(issue(state))) as client:
        return await Integrations(service,client).import_linear('ENG-1')


def comment_payload(body, cid='comment-1', **data):
    return {'organizationId':'workspace-one','type':'Comment','action':'create',
            'actor':{'type':'user','name':'You'},
            'data':{'id':cid,'issueId':'issue-one','body':body,**data}}


async def test_linear_import_is_explicit_and_deduplicated(linear_service):
    async with httpx.AsyncClient(transport=gql_transport(issue('todo'))) as client:
        integration = Integrations(linear_service,client)
        first = await integration.import_linear('ENG-1')
        second = await integration.import_linear('ENG-1')
    assert first['id'] == second['id']
    assert len(linear_service.list_tasks()) == 1
    assert len(linear_service.get_task(first['id'])['runs']) == 1
    assert not linear_service.store.all("SELECT * FROM outbox WHERE kind='linear.sync'")


async def test_linear_import_waiting_requires_question(linear_service):
    async with httpx.AsyncClient(transport=gql_transport(issue('waiting_on_input'))) as client:
        integration = Integrations(linear_service,client)
        with pytest.raises(DomainError,match='question'):
            await integration.import_linear('ENG-1')
        assert not linear_service.list_tasks()
        task = await integration.import_linear('ENG-1',question='Which requirement?')
    assert task['blocker_comment_id']


async def test_linear_import_in_progress_is_queued_not_claimed(linear_service):
    task = await imported(linear_service,'in_progress')
    assert task['state'] == 'todo'
    assert linear_service.get_task(task['id'])['runs'][0]['status'] == 'queued'


async def test_linear_ignores_unimported_issues(linear_service):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: (_ for _ in ()).throw(AssertionError('No request expected')))) as client:
        await Integrations(linear_service,client).handle_linear(
            {'organizationId':'workspace-one','type':'Issue','action':'create','data':{'id':'not-linked'}})
    assert not linear_service.list_tasks()


async def test_linear_status_update_queues_and_deduplicates(linear_service):
    task = await imported(linear_service)
    remote = issue('todo',updated='2026-10-16T12:01:00.000Z')
    async with httpx.AsyncClient(transport=gql_transport(remote)) as client:
        integration = Integrations(linear_service,client)
        payload = {'organizationId':'workspace-one','type':'Issue','action':'update','data':{'id':'issue-one'}}
        await integration.handle_linear(payload)
        await integration.handle_linear(payload)
    current = linear_service.get_task(task['id'])
    assert current['state'] == 'todo' and len(current['runs']) == 1


async def test_linear_old_snapshot_does_not_revert(linear_service):
    task = await imported(linear_service,'todo')
    async with httpx.AsyncClient(transport=gql_transport(issue('planning',updated='2026-10-15T12:00:00.000Z'))) as client:
        await Integrations(linear_service,client).handle_linear(
            {'organizationId':'workspace-one','type':'Issue','action':'update','data':{'id':'issue-one'}})
    assert linear_service.get_task(task['id'])['state'] == 'todo'


async def test_linear_silent_waiting_move_corrected(linear_service):
    task = await imported(linear_service,'todo')
    remote = issue('waiting_on_input',updated='2026-10-16T12:01:00.000Z')
    async with httpx.AsyncClient(transport=gql_transport(remote)) as client:
        await Integrations(linear_service,client).handle_linear(
            {'organizationId':'workspace-one','type':'Issue','action':'update','data':{'id':'issue-one'}})
    current = linear_service.get_task(task['id'])
    assert current['state'] == 'todo'
    assert '/wait' in current['comments'][0]['body']
    assert linear_service.store.all("SELECT * FROM outbox WHERE kind='linear.sync'")


async def test_linear_wait_resume_commands_and_mirror_echo(linear_service):
    task = await imported(linear_service,'todo')
    async with httpx.AsyncClient() as client:
        integration = Integrations(linear_service,client)
        await integration.handle_linear(comment_payload('/wait Which option?'))
        current = linear_service.get_task(task['id'])
        assert current['state'] == 'waiting_on_input'
        assert current['comments'][0]['id'] == current['blocker_comment_id']
        await integration.handle_linear(comment_payload('Just a note','comment-2'))
        assert not any(r['status']=='queued' for r in linear_service.get_task(task['id'])['runs'])
        await integration.handle_linear(comment_payload('/resume Option A','comment-3'))
        await integration.handle_linear(comment_payload('/resume Option A','comment-3'))
        current = linear_service.get_task(task['id'])
        assert sum(r['status']=='queued' for r in current['runs']) == 1
        # A mirrored agent comment can never wake the worker again.
        await integration.handle_linear(comment_payload(f'<!-- acp:{current["comments"][0]["id"]} -->\nquestion','mirror-1'))
        assert sum(r['status']=='queued' for r in linear_service.get_task(task['id'])['runs']) == 1


async def test_linear_threaded_answer_resumes(linear_service):
    task = await imported(linear_service,'todo')
    async with httpx.AsyncClient() as client:
        integration = Integrations(linear_service,client)
        await integration.handle_linear(comment_payload('/wait Which option?'))
        await integration.handle_linear(comment_payload('Option B','comment-2',parentId='comment-1'))
    current = linear_service.get_task(task['id'])
    assert sum(r['status']=='queued' for r in current['runs']) == 1
    assert current['comments'][-1]['in_reply_to'] == current['blocker_comment_id']


async def test_linear_comment_edits_publish_but_do_not_retrigger(linear_service):
    task = await imported(linear_service)
    async with httpx.AsyncClient() as client:
        integration = Integrations(linear_service,client)
        await integration.handle_linear(comment_payload('First'))
        payload = comment_payload('Edited'); payload['action']='update'
        await integration.handle_linear(payload)
    assert linear_service.get_task(task['id'])['comments'][0]['body']=='Edited'
    assert linear_service.store.events()[-1]['type']=='comment.updated'
    assert not linear_service.get_task(task['id'])['runs']


async def test_linear_push_uses_latest_local_state_and_records_echo_timestamp(linear_service):
    task = await imported(linear_service)
    linear_service.move_task(task['id'],'todo')
    captures=[]
    def respond(request):
        captures.append(json.loads(request.content))
        return httpx.Response(200,json={'data':{'issueUpdate':{'success':True,'issue':{'updatedAt':'2026-10-16T12:02:00.000Z'}}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await Integrations(linear_service,client).sync_linear_task(task['id'])
    assert captures[0]['variables']['input']['stateId']=='state-todo'
    assert linear_service.get_task(task['id'])['linear_updated_at']=='2026-10-16T12:02:00.000Z'


async def test_linear_comment_recovers_uncertain_success_without_duplicate(linear_service):
    task = await imported(linear_service)
    comment = linear_service.add_comment(task['id'],'Local plan')
    def respond(request):
        data=json.loads(request.content)
        assert 'comments(first:100' in data['query'], 'No create needed if the marker already exists remotely'
        return httpx.Response(200,json={'data':{'issue':{'comments':{'nodes':[{'id':'remote-1','body':f'<!-- acp:{comment["id"]} -->\nLocal plan'}],
                                                                   'pageInfo':{'hasNextPage':False,'endCursor':None}}}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        await Integrations(linear_service,client).sync_linear_comment(task['id'],comment['id'])
    assert linear_service.get_task(task['id'])['comments'][0]['external_id']=='linear:remote-1'


def review_envelope(state='changes_requested', review_id=11):
    return {'event':'pull_request_review','payload':{'action':'submitted',
            'pull_request':{'html_url':'https://github.com/example/project/pull/7'},
            'review':{'id':review_id,'state':state,'body':'Fix the edge case',
                      'html_url':'https://github.com/example/project/pull/7#pullrequestreview-11','commit_id':'head-sha'}}}


async def test_github_requested_changes_fetches_inline_context_and_resumes_once(service):
    task,_=reviewed(service,pr=True)
    def respond(request):
        assert request.url.path.endswith('/pulls/7/reviews/11/comments')
        return httpx.Response(200,json=[{'path':'app.py','line':42,'body':'Handle None','diff_hunk':'@@ function','html_url':'https://github.com/example/project/pull/7#discussion'}])
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        integration=Integrations(service,client)
        await integration.handle_github(review_envelope())
        await integration.handle_github(review_envelope())
    current=service.get_task(task['id'])
    assert len(current['runs'])==2
    assert current['runs'][0]['inputs'][0]['inline_comments'][0]['body']=='Handle None'
    assert current['state']=='ready_for_review'  # moves on actual start, not on webhook receipt
    assert sum(e['type']=='pr.review_submitted' for e in service.store.events())==1


@pytest.mark.parametrize('state',['approved','commented'])
async def test_github_other_reviews_do_not_run_or_merge(service,state):
    task,_=reviewed(service,pr=True)
    async with httpx.AsyncClient() as client:
        await Integrations(service,client).handle_github(review_envelope(state))
    assert service.claim_next() is None
    assert service.get_task(task['id'])['state']=='ready_for_review'


async def test_github_unlinked_pr_ignored(service):
    async with httpx.AsyncClient() as client:
        await Integrations(service,client).handle_github(review_envelope())
    assert not service.list_tasks()


async def test_github_merge_moves_to_deploy_not_done(service):
    task,_=reviewed(service,pr=True)
    envelope={'event':'pull_request','payload':{'action':'closed','pull_request':{
        'html_url':'https://github.com/example/project/pull/7','merged':True,'closed_at':'2026-10-16T13:00:00Z'}}}
    async with httpx.AsyncClient() as client:
        await Integrations(service,client).handle_github(envelope)
    assert service.get_task(task['id'])['state']=='deploy'


def sign(secret, raw, prefix=''):
    return prefix+hmac.new(secret.encode(),raw,hashlib.sha256).hexdigest()


def test_github_webhook_verifies_raw_body_and_deduplicates(settings):
    settings.github_webhook_secret='secret'
    with TestClient(create_app(settings,start_worker=False)) as client:
        raw=json.dumps(review_envelope()['payload']).encode()
        headers={'X-GitHub-Delivery':'delivery-1','X-GitHub-Event':'pull_request_review',
                 'X-Hub-Signature-256':sign('secret',raw,'sha256=')}
        response=client.post('/hooks/github',content=raw,headers=headers)
        assert response.status_code==202 and not response.json()['duplicate']
        assert client.post('/hooks/github',content=raw,headers=headers).json()['duplicate']
        assert client.post('/hooks/github',content=raw+b' ',headers=headers).status_code==401


def test_linear_webhook_timestamp_workspace_and_signature(linear_service):
    settings=linear_service.settings
    with TestClient(create_app(settings,start_worker=False,hooks_only=True)) as client:
        def send(**fields):
            payload={'organizationId':'workspace-one','webhookTimestamp':int(time.time()*1000),**fields}
            raw=json.dumps(payload).encode()
            return client.post('/hooks/linear',content=raw,headers={
                'Linear-Delivery':str(time.time_ns()),'Linear-Signature':sign('linear-secret',raw)})
        assert send().status_code==200
        assert send(webhookTimestamp=int((time.time()-120)*1000)).status_code==401
        assert send(organizationId='different-workspace').status_code==422
        assert client.get('/api/tasks').status_code==404
        assert client.get('/docs').status_code==404
        assert client.get('/').status_code==404


def test_unconfigured_webhook_rejected(client):
    assert client.post('/hooks/github',json={}).status_code==503


def test_workspace_switch_requires_new_database(linear_service):
    from acp.service import TaskService
    linear_service.settings.linear_workspace_id='new-workspace'
    with pytest.raises(ValueError,match='another Linear workspace'):
        TaskService(linear_service.store,linear_service.settings)


async def test_invalid_linear_wait_command_retains_comment(linear_service):
    task = await imported(linear_service, 'planning')
    async with httpx.AsyncClient() as client:
        await Integrations(linear_service, client).handle_linear(comment_payload('/wait Invalid lane'))
    current = linear_service.get_task(task['id'])
    assert current['state'] == 'planning'
    assert current['comments'][0]['body'] == '/wait Invalid lane'
    assert 'Cannot move' in current['comments'][1]['body']


def test_nonfinite_signed_linear_timestamp_is_rejected(linear_service):
    data = {'organizationId':'workspace-one', 'webhookTimestamp':'NaN'}
    raw = json.dumps(data).encode()
    signature = hmac.new(b'linear-secret', raw, hashlib.sha256).hexdigest()
    with TestClient(create_app(linear_service.settings, hooks_only=True)) as client:
        response = client.post('/hooks/linear', content=raw,
                               headers={'Linear-Signature':signature, 'Linear-Delivery':'nan-test'})
    assert response.status_code == 401


async def test_acp_label_imports_removal_stops_work_and_readdition_reuses_card(linear_service):
    linear_service.settings.linear_label_sync = True
    linear_service.settings.linear_provider = 'demo'
    remote = issue('todo', labels={'nodes': [{'name': 'acp'}]})
    async with httpx.AsyncClient(transport=gql_transport(remote)) as client:
        integration = Integrations(linear_service, client)
        event = {'organizationId': 'workspace-one', 'type': 'Issue', 'action': 'update', 'data': {'id': 'issue-one'}}
        await integration.handle_linear(event)
        tasks = linear_service.list_tasks()
        assert len(tasks) == 1
        task = tasks[0]
        assert task['provider'] == 'demo'
        assert len(linear_service.get_task(task['id'])['runs']) == 1
        run = linear_service.claim_next()
        linear_service.mark_started(run['id'])
        remote['labels']['nodes'] = []
        await integration.handle_linear(event)
        assert linear_service.list_tasks() == []
        assert linear_service.store.one('SELECT stop_requested FROM runs WHERE id=?', (run['id'],))['stop_requested'] == 1
        linear_service.finish_run(run['id'])
        remote['labels']['nodes'] = [{'name': 'ACP'}]
        await integration.handle_linear(event)
        restored = linear_service.list_tasks()
        assert len(restored) == 1 and restored[0]['id'] == task['id']
        assert sum(r['status'] == 'queued' for r in linear_service.get_task(task['id'])['runs']) == 1


async def test_unlabelled_issue_never_starts_work_in_label_mode(linear_service):
    linear_service.settings.linear_label_sync = True
    async with httpx.AsyncClient(transport=gql_transport(issue(labels={'nodes': []}))) as client:
        await Integrations(linear_service, client).handle_linear(
            {'organizationId': 'workspace-one', 'type': 'Issue', 'action': 'create', 'data': {'id': 'issue-one'}})
    assert linear_service.list_tasks() == []


async def test_label_scan_imports_existing_issues_and_recovers_missed_removals(linear_service):
    linear_service.settings.linear_label_sync = True
    linear_service.settings.linear_provider = 'demo'
    remote = issue('planning', labels={'nodes': [{'name': 'acp'}]})
    items = [remote]
    def respond(request):
        body = json.loads(request.content)
        if 'ACPLabelled' in body['query']:
            return httpx.Response(200, json={'data': {'organization': {'id': 'workspace-one'},
                'team': {'issues': {'nodes': items, 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}})
        if 'ACPComments' in body['query']:
            return httpx.Response(200, json={'data': {'issue': {'comments': {'nodes': [], 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}})
        return httpx.Response(200, json={'data': {'organization': {'id': 'workspace-one'}, 'issue': remote}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        integration = Integrations(linear_service, client)
        await integration.sync_labelled()
        assert len(linear_service.list_tasks()) == 1
        items.clear()
        remote['labels']['nodes'] = []
        await integration.sync_labelled()
        assert linear_service.list_tasks() == []


async def test_hidden_task_cannot_be_requeued_from_stale_dialog(linear_service):
    task = await imported(linear_service)
    async with httpx.AsyncClient() as client:
        Integrations(linear_service, client).hide_linear(task['id'])
    with pytest.raises(DomainError, match='label'):
        linear_service.move_task(task['id'], 'todo')
    assert linear_service.claim_next() is None


async def test_issue_moved_out_of_team_cancels_and_hides(linear_service):
    task = await imported(linear_service, 'todo')
    linear_service.settings.linear_label_sync = True
    run = linear_service.claim_next()
    linear_service.mark_started(run['id'])
    remote = issue('todo', team={'id': 'other-team'}, labels={'nodes': []})
    async with httpx.AsyncClient(transport=gql_transport(remote)) as client:
        await Integrations(linear_service, client).handle_linear(
            {'organizationId': 'workspace-one', 'type': 'Issue', 'action': 'update',
             'data': {'id': 'issue-one'}})
    assert linear_service.list_tasks() == []
    assert linear_service.store.one('SELECT stop_requested FROM runs WHERE id=?', (run['id'],))['stop_requested'] == 1


@pytest.mark.parametrize("updated_offset", [0, -0.020])
async def test_polling_recovers_human_reply_without_replaying_old_or_bot_commands(linear_service, updated_offset):
    from datetime import datetime, timezone
    task = await imported(linear_service, 'todo')
    linear_service.request_input(task['id'], 'Which option?')
    now = time.time()
    def stamp(t): return datetime.fromtimestamp(t, timezone.utc).isoformat()
    comments = [
        {'id': 'old', 'body': '/resume old answer', 'createdAt': stamp(now-3600), 'updatedAt': stamp(now-3600), 'user': {'name': 'You'}, 'botActor': None, 'parent': None},
        {'id': 'bot', 'body': '/resume bot answer', 'createdAt': stamp(now+1), 'updatedAt': stamp(now+1), 'user': {'name': 'Bot'}, 'botActor': {'__typename': 'ActorBot'}, 'parent': None},
        {'id': 'human', 'body': '/resume human answer', 'createdAt': stamp(now+2), 'updatedAt': stamp(now+2+updated_offset), 'user': {'name': 'You'}, 'botActor': None, 'parent': None},
    ]
    def respond(request):
        return httpx.Response(200, json={'data': {'issue': {'comments': {'nodes': comments, 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        integration = Integrations(linear_service, client)
        await integration.poll_comments(task['id'])
        await integration.poll_comments(task['id'])
    current = linear_service.get_task(task['id'])
    queued = [r for r in current['runs'] if r['status']=='queued']
    assert len(queued)==1
    assert len(queued[0]['inputs'])==1
    assert next(c for c in current['comments'] if c['id']==queued[0]['inputs'][0]['comment_id'])['body']=='/resume human answer'
    assert not any(c['body']=='/resume old answer' for c in current['comments'])


async def test_stale_foreign_team_event_uses_current_issue_state(linear_service):
    task = await imported(linear_service, 'todo')
    linear_service.settings.linear_label_sync = True
    remote = issue('todo', labels={'nodes': [{'name': 'acp'}]})
    async with httpx.AsyncClient(transport=gql_transport(remote)) as client:
        await Integrations(linear_service, client).handle_linear(
            {'organizationId': 'workspace-one', 'type': 'Issue', 'action': 'update',
             'data': {'id': 'issue-one', 'teamId': 'old-team'}})
    assert len(linear_service.list_tasks()) == 1
    assert linear_service.get_task(task['id'])['hidden'] == 0


async def test_labelled_issue_requires_board_owner_and_reassignment_stops_work(linear_service):
    linear_service.settings.linear_label_sync = True
    linear_service.settings.linear_user_id = 'owner-one'
    linear_service.settings.linear_provider = 'demo'
    remote = issue('todo', labels={'nodes': [{'name': 'acp'}]}, assignee={'id': 'someone-else'})
    async with httpx.AsyncClient(transport=gql_transport(remote)) as client:
        integration = Integrations(linear_service, client)
        event = {'organizationId': 'workspace-one', 'type': 'Issue', 'action': 'update', 'data': {'id': 'issue-one'}}
        await integration.handle_linear(event)
        assert not linear_service.list_tasks()
        remote['assignee'] = {'id': 'owner-one'}
        await integration.handle_linear(event)
        task = linear_service.list_tasks()[0]
        run = linear_service.claim_next()
        linear_service.mark_started(run['id'])
        remote['assignee'] = None
        await integration.handle_linear(event)
        assert not linear_service.list_tasks()
        assert linear_service.store.one('SELECT stop_requested FROM runs WHERE id=?', (run['id'],))['stop_requested'] == 1
        assert linear_service.get_task(task['id'])['hidden'] == 1


async def test_reply_after_reassignment_before_scan_resumes_restored_task(linear_service):
    from datetime import datetime, timezone
    task = await imported(linear_service, 'todo')
    linear_service.request_input(task['id'], 'Which option?')
    linear_service.settings.linear_label_sync = True
    now = time.time()
    def stamp(t): return datetime.fromtimestamp(t, timezone.utc).isoformat()
    with linear_service.store.transaction() as db:
        db.execute('UPDATE tasks SET created_at=? WHERE id=?', (now-120, task['id']))
    remote = issue('waiting_on_input', updated=stamp(now-30), labels={'nodes': [{'name': 'acp'}]})
    comments = [
        {'id': 'hidden-reply', 'body': '/resume while hidden', 'createdAt': stamp(now-60), 'updatedAt': stamp(now-60), 'user': {'name': 'You'}, 'botActor': None, 'parent': None},
        {'id': 'restored-reply', 'body': '/resume after reassignment', 'createdAt': stamp(now-10), 'updatedAt': stamp(now-10), 'user': {'name': 'You'}, 'botActor': None, 'parent': None},
    ]
    def respond(request):
        if 'ACPComments' in json.loads(request.content)['query']:
            return httpx.Response(200, json={'data': {'issue': {'comments': {'nodes': comments, 'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}})
        return httpx.Response(200, json={'data': {'organization': {'id': 'workspace-one'}, 'issue': remote}})
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        integration = Integrations(linear_service, client)
        integration.hide_linear(task['id'])
        await integration.handle_linear({'organizationId': 'workspace-one', 'type': 'Issue', 'action': 'update', 'data': {'id': 'issue-one'}})
        await integration.poll_comments(task['id'])
    current = linear_service.get_task(task['id'])
    assert current['state'] == 'waiting_on_input'
    assert sum(r['status'] == 'queued' for r in current['runs']) == 1
    assert any(c['body'] == '/resume after reassignment' for c in current['comments'])
    assert not any(c['body'] == '/resume while hidden' for c in current['comments'])
