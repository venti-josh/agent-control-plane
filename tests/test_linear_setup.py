import hashlib
import hmac
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from acp.api import create_app
from acp.config import Settings


@pytest.fixture
def linear_remote(monkeypatch):
    """Only the external GraphQL transport is replaced; setup and persistence are real."""
    states = [
        {'id': 'backlog', 'name': 'Backlog', 'type': 'backlog'},
        {'id': 'todo', 'name': 'Todo', 'type': 'unstarted'},
        {'id': 'started', 'name': 'In Progress', 'type': 'started'},
        {'id': 'done', 'name': 'Done', 'type': 'completed'},
    ]
    remote = {'states': states, 'hooks': [], 'workspace': 'workspace-one', 'calls': []}

    async def send(self, request, *args, **kwargs):
        body = json.loads(request.content)
        query, variables = body['query'], body.get('variables', {})
        remote['calls'].append(body)
        if request.headers.get('Authorization') != 'test-key':
            return httpx.Response(401, request=request, json={'error': 'bad key'})
        if 'ACPDiscover' in query:
            data = {'organization': {'id': remote['workspace'], 'name': 'Example'},
                    'viewer': {'id': remote.get('owner', 'owner-one'), 'name': 'Test Owner'},
                    'teams': {'nodes': [{'id': 'team-one', 'name': 'Engineering', 'key': 'ENG'}],
                              'pageInfo': {'hasNextPage': False, 'endCursor': None}}}
        elif 'ACPStates' in query:
            data = {'team': {'id': 'team-one', 'states': {'nodes': states,
                            'pageInfo': {'hasNextPage': False, 'endCursor': None}}}}
        elif 'workflowStateCreate' in query:
            item = variables['input']
            assert item['teamId'] == 'team-one'
            state = {'id': 'created-' + str(len(states)), 'name': item['name'], 'type': item['type']}
            states.append(state)
            data = {'workflowStateCreate': {'success': True, 'workflowState': state}}
        elif 'ACPLabels' in query:
            data = {'issueLabels': {'nodes': remote.setdefault('labels', []),
                                   'pageInfo': {'hasNextPage': False, 'endCursor': None}}}
        elif 'issueLabelCreate' in query:
            label = {**variables['input'], 'id': 'label-acp', 'team': {'id': 'team-one'}}
            remote['labels'].append(label)
            data = {'issueLabelCreate': {'success': True, 'issueLabel': label}}
        elif 'ACPWebhooks' in query:
            data = {'webhooks': {'nodes': remote['hooks'],
                                'pageInfo': {'hasNextPage': False, 'endCursor': None}}}
        elif 'webhookCreate' in query:
            item = variables['input']
            assert item['resourceTypes'] == ['Issue', 'Comment']
            hook = {**item, 'id': item['id'], 'team': {'id': item['teamId']}}
            remote['hooks'].append(hook)
            data = {'webhookCreate': {'success': True, 'webhook': {'id': hook['id']}}}
        else:
            raise AssertionError(body)
        return httpx.Response(200, request=request, json={'data': data})

    monkeypatch.setattr(httpx.AsyncClient, 'send', send)
    return remote


def configure(client, **kwargs):
    return client.post('/api/integrations/linear/configure', json={
        'api_key': 'test-key', 'team_id': 'team-one', 'create_missing': True, **kwargs})


def test_guided_setup_discovers_maps_saves_and_survives_restart(settings, linear_remote):
    with TestClient(create_app(settings, start_worker=False)) as client:
        discovery = client.post('/api/integrations/linear/discover', json={'api_key': 'test-key'})
        assert discovery.status_code == 200
        assert discovery.json()['teams'][0]['suggested_states']['planning'] == 'backlog'
        assert discovery.json()['teams'][0]['missing'] == ['waiting_on_input', 'ready_for_review', 'deploy']
        assert not (settings.data_dir / 'linear.json').exists()
        saved = configure(client)
        assert saved.status_code == 200, saved.text
        result = saved.json()
        assert result['configured'] is True
        assert len(set(result['states'].values())) == 7
        assert result['states']['planning'] == 'backlog'
        assert 'test-key' not in saved.text
        assert client.get('/api/config').json()['linear_enabled'] is True
        assert configure(client).status_code == 200
        assert len(linear_remote['states']) == 7
    path = settings.data_dir / 'linear.json'
    assert path.stat().st_mode & 0o777 == 0o600
    fresh = Settings(data_dir=settings.data_dir)
    with TestClient(create_app(fresh, start_worker=False)) as client:
        assert fresh.linear_api_key == 'test-key'
        assert client.get('/api/integrations/linear/status').json()['configured'] is True


def test_setup_rejects_invalid_key_mapping_and_workspace_without_persisting(settings, linear_remote):
    with TestClient(create_app(settings, start_worker=False)) as client:
        assert configure(client, api_key='bad-key').status_code == 422
        assert configure(client, create_missing=False).status_code == 422
        assert configure(client, states={'planning': 'foreign-state'}).status_code == 422
        assert not (settings.data_dir / 'linear.json').exists()
        assert configure(client).status_code == 200
        original = (settings.data_dir / 'linear.json').read_bytes()
        linear_remote['workspace'] = 'other-workspace'
        assert configure(client).status_code == 409
        assert (settings.data_dir / 'linear.json').read_bytes() == original


def test_webhook_registration_retries_reuse_secret_and_receipt_proves_delivery(settings, linear_remote):
    # Start a separate listener before setup to verify hot reload across processes.
    with TestClient(create_app(Settings(data_dir=settings.data_dir), hooks_only=True)) as hooks:
        with TestClient(create_app(settings, start_worker=False)) as client:
            assert configure(client).status_code == 200
            url = 'https://acp.example.com/hooks/linear'
            response = client.post('/api/integrations/linear/webhook', json={'url': url})
            assert response.status_code == 200, response.text
            assert response.json()['webhook_url'] == url
            assert response.json()['last_webhook_at'] is None
            assert client.post('/api/integrations/linear/webhook', json={'url': url}).status_code == 200
            assert len(linear_remote['hooks']) == 1
            secret = json.loads((settings.data_dir / 'linear.json').read_text())['linear_webhook_secret']
            assert secret not in response.text
            raw = json.dumps({'organizationId': 'workspace-one', 'type': 'Issue', 'action': 'update',
                              'data': {'id': 'unimported'}, 'webhookTimestamp': time.time() * 1000}).encode()
            headers = {'Linear-Delivery': 'delivery-one',
                       'Linear-Signature': hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()}
            for duplicate in (False, True):
                accepted = hooks.post('/hooks/linear', content=raw, headers=headers)
                assert accepted.status_code == 200
                assert accepted.json()['duplicate'] is duplicate
            status = client.get('/api/integrations/linear/status').json()
            assert status['last_webhook_at'] > 0
            assert hooks.get('/api/integrations/linear/status').status_code == 404
            assert hooks.post('/api/integrations/linear/configure', json={}).status_code == 404


def test_setup_errors_never_echo_secrets_and_reject_cross_origin(settings, linear_remote):
    with TestClient(create_app(settings, start_worker=False)) as client:
        response = client.post('/api/integrations/linear/discover', json={'api_key': 'private-key'})
        assert response.status_code == 422
        assert 'private-key' not in response.text
        response = client.post('/api/integrations/linear/configure',
                               json={'api_key': 'test-key', 'team_id': 'team-one'},
                               headers={'Origin': 'https://evil.example'})
        assert response.status_code == 403


def test_webhook_setup_can_recover_changed_tunnel_after_remote_removal(settings, linear_remote):
    with TestClient(create_app(settings, start_worker=False)) as client:
        assert configure(client).status_code == 200
        assert client.post('/api/integrations/linear/webhook', json={'url': 'https://old.example/hooks/linear'}).status_code == 200
        linear_remote['hooks'].clear()
        response = client.post('/api/integrations/linear/webhook', json={'url': 'https://new.example/hooks/linear'})
        assert response.status_code == 200
        assert response.json()['webhook_url'] == 'https://new.example/hooks/linear'


def test_setup_creates_acp_label_once(settings, linear_remote):
    with TestClient(create_app(settings, start_worker=False)) as client:
        assert configure(client).status_code == 200
        assert configure(client).status_code == 200
    assert len(linear_remote.get('labels', [])) == 1
    assert linear_remote['labels'][0]['name'] == 'acp'


def test_setup_validation_does_not_echo_secret_values(client):
    response = client.post('/api/integrations/linear/discover', json={'api_key': {'secret': 'do-not-echo'}})
    assert response.status_code == 422
    assert 'do-not-echo' not in response.text


def test_board_is_pinned_to_api_key_owner(settings, linear_remote):
    with TestClient(create_app(settings, start_worker=False)) as client:
        response = configure(client)
        assert response.status_code == 200
        assert response.json()['user_id'] == 'owner-one'
        assert response.json()['user_name'] == 'Test Owner'
        original = (settings.data_dir / 'linear.json').read_bytes()
        linear_remote['owner'] = 'owner-two'
        assert configure(client).status_code == 409
        assert (settings.data_dir / 'linear.json').read_bytes() == original
