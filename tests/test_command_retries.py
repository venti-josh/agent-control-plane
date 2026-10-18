from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient
from acp.api import create_app


def test_create_retry_after_restart_returns_original_result(settings):
    body = {'title': 'One task', 'state': 'todo'}
    headers = {'Idempotency-Key': 'create-1'}
    with TestClient(create_app(settings, start_worker=False)) as client:
        first = client.post('/api/tasks', json=body, headers=headers)
    with TestClient(create_app(settings, start_worker=False)) as client:
        replay = client.post('/api/tasks', json=body, headers=headers)
        assert replay.json() == first.json()
        assert len(client.get('/api/tasks').json()) == 1
        assert len(client.get('/api/tasks/' + first.json()['id']).json()['runs']) == 1


def test_key_cannot_be_reused_for_different_command(client):
    headers = {'Idempotency-Key': 'shared-key'}
    assert client.post('/api/tasks', json={'title': 'First'}, headers=headers).status_code == 201
    conflict = client.post('/api/tasks', json={'title': 'Different'}, headers=headers)
    assert conflict.status_code == 409
    assert len(client.get('/api/tasks').json()) == 1


def test_concurrent_create_retries_commit_once(client):
    def send(_):
        return client.post('/api/tasks', json={'title': 'Concurrent'},
                           headers={'Idempotency-Key': 'concurrent'}).json()
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(send, range(4)))
    assert len({r['id'] for r in results}) == 1
    assert len(client.get('/api/tasks').json()) == 1


def test_replayed_approval_after_claim_does_not_queue_another_run(client):
    service = client.app.state.service
    task = service.create_task('Waiting', state='todo')
    run = service.claim_next()
    service.mark_started(run['id'])
    service.request_input(task['id'], 'Continue?', run['id'])
    service.finish_run(run['id'])
    path = '/api/tasks/' + task['id'] + '/comments'
    headers = {'Idempotency-Key': 'approval-1'}
    body = {'body': 'Approved', 'resume': True, 'in_reply_to': service.get_task(task['id'])['blocker_comment_id']}
    first = client.post(path, json=body, headers=headers)
    following = service.claim_next()
    service.mark_started(following['id'])
    replay = client.post(path, json=body, headers=headers)
    assert replay.status_code == 201
    assert replay.json() == first.json()
    result = service.get_task(task['id'])
    assert len(result['runs']) == 2
    assert sum(c['body'] == 'Approved' for c in result['comments']) == 1


def test_review_retry_after_run_completion_returns_receipt_without_mutation(client):
    service = client.app.state.service
    task = service.create_task('Review', state='todo')
    run = service.claim_next(); service.mark_started(run['id'])
    path = '/api/tasks/' + task['id'] + '/submit-for-review'
    body = {'summary': 'Verified', 'run_id': run['id']}
    headers = {'Idempotency-Key': 'review-1'}
    first = client.post(path, json=body, headers=headers)
    service.finish_run(run['id'])
    replay = client.post(path, json=body, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    result = service.get_task(task['id'])
    assert len(result['artifacts']) == 1
    assert sum(c['body'] == 'Verified' for c in result['comments']) == 1
    assert result['runs'][0]['status'] == 'succeeded'
    assert client.post(path, json=body, headers={'Idempotency-Key': 'new-review'}).status_code == 409


def test_failed_command_does_not_consume_key(client):
    headers = {'Idempotency-Key': 'retry-validation'}
    assert client.post('/api/tasks', json={'title': '   '}, headers=headers).status_code == 422
    assert client.post('/api/tasks', json={'title': 'Valid'}, headers=headers).status_code == 201


def test_desktop_review_receipt_replays_after_acknowledgement(client):
    from acp.desktop import DesktopBridge
    from uuid import uuid4
    service = client.app.state.service
    bridge = DesktopBridge(service)
    task = service.create_task('Desktop', provider='codex', state='todo', repo_path='/tmp')
    run = service.claim_next(); bridge.dispatch(run['id']); bridge.claim(run['id'])
    thread = str(uuid4()); bridge.started(run['id'], thread)
    path = '/api/desktop/runs/' + run['id'] + '/action'
    body = {'thread_id': thread, 'action': 'review', 'text': 'Verified once'}
    headers = {'Idempotency-Key': 'desktop-review'}
    first = client.post(path, json=body, headers=headers)
    bridge.complete(run['id'], thread)
    replay = client.post(path, json=body, headers=headers)
    assert replay.status_code == 200
    assert replay.json() == first.json()
    assert len(service.get_task(task['id'])['artifacts']) == 1
    body['thread_id'] = str(uuid4())
    assert client.post(path, json=body, headers=headers).status_code == 409


def test_cli_retries_lost_reply_with_same_receipt(client, monkeypatch):
    import httpx
    from acp import cli
    attempts = []
    def transport(request):
        if request.url.path == '/api/health':
            return httpx.Response(200, json={'capabilities': ['command-receipts']})
        result = client.post(request.url.path, json={'title': 'Transport'}, headers=dict(request.headers))
        attempts.append(result.json()['id'])
        if len(attempts) == 1:
            raise httpx.ReadTimeout('Response lost after commit')
        return httpx.Response(result.status_code, json=result.json())
    real_client = httpx.Client
    monkeypatch.setattr(cli.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(transport)))
    result = cli.request('http://localhost', 'POST', '/api/tasks', {'title': 'Transport'})
    assert attempts == [result['id'], result['id']]
    assert len(client.get('/api/tasks').json()) == 1


def test_cli_does_not_retry_mutations_against_old_server(monkeypatch):
    import httpx
    from acp import cli
    attempts = []
    def transport(request):
        if request.url.path == '/api/health':
            return httpx.Response(200, json={'version': '0.1.3'})
        attempts.append(request)
        raise httpx.ReadTimeout('Unknown outcome')
    real_client = httpx.Client
    monkeypatch.setattr(cli.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(transport)))
    import pytest
    with pytest.raises(RuntimeError, match='does not support idempotent commands'):
        cli.request('http://localhost', 'POST', '/api/tasks', {'title': 'Transport'})
    assert len(attempts) == 0


def test_cli_does_not_replay_if_server_capability_disappears(client, monkeypatch):
    import httpx
    import pytest
    from acp import cli
    health_checks, writes = [], []
    def transport(request):
        if request.url.path == '/api/health':
            health_checks.append(1)
            return httpx.Response(200, json={'capabilities': ['command-receipts'] if len(health_checks) == 1 else []})
        result = client.post(request.url.path, json={'title': 'Upgrade boundary'}, headers=dict(request.headers))
        writes.append(result.json()['id'])
        raise httpx.ReadTimeout('Committed but reply lost')
    real_client = httpx.Client
    monkeypatch.setattr(cli.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(transport)))
    with pytest.raises((RuntimeError, httpx.ReadTimeout)):
        cli.request('http://localhost', 'POST', '/api/tasks', {'title': 'Upgrade boundary'})
    assert len(writes) == 1
    assert len(client.get('/api/tasks').json()) == 1


def test_cli_separate_invocations_reuse_unresolved_command(client, monkeypatch, tmp_path):
    import httpx
    import pytest
    from acp import cli
    monkeypatch.setenv('ACP_COMMAND_JOURNAL', str(tmp_path / 'commands.sqlite3'))
    monkeypatch.setattr(cli.time, 'sleep', lambda _: None)
    writes, lose = [], [True]
    def transport(request):
        if request.url.path == '/api/health':
            return httpx.Response(200, json={'capabilities': ['command-receipts']})
        result = client.post(request.url.path, json={'title': 'Persistent intent'}, headers=dict(request.headers))
        writes.append((request.headers['Idempotency-Key'], result.json()['id']))
        if lose[0]: raise httpx.ReadTimeout('Committed but reply lost')
        return httpx.Response(result.status_code, json=result.json())
    real_client = httpx.Client
    monkeypatch.setattr(cli.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(transport)))
    with pytest.raises((RuntimeError, httpx.ReadTimeout)):
        cli.request('http://localhost', 'POST', '/api/tasks', {'title': 'Persistent intent'})
    lose[0] = False
    result = cli.request('http://localhost', 'POST', '/api/tasks', {'title': 'Persistent intent'})
    assert len(set(writes)) == 1
    assert len(client.get('/api/tasks').json()) == 1
    # Once the outcome is delivered, the same payload can express a genuinely new intent.
    following = cli.request('http://localhost', 'POST', '/api/tasks', {'title': 'Persistent intent'})
    assert following['id'] != result['id']


def test_mcp_restarted_call_reuses_unresolved_comment(client, monkeypatch, tmp_path):
    import httpx
    import pytest
    from acp import cli, mcp_server
    service = client.app.state.service
    task = service.create_task('MCP retry', state='todo'); run = service.claim_next(); service.mark_started(run['id'])
    for key, value in {'ACP_COMMAND_JOURNAL':str(tmp_path / 'commands.sqlite3'), 'ACP_URL':'http://localhost',
                       'ACP_TASK_ID':task['id'], 'ACP_RUN_ID':run['id']}.items(): monkeypatch.setenv(key,value)
    monkeypatch.setattr(cli.time, 'sleep', lambda _: None)
    lose = [True]
    def transport(request):
        import json
        if request.url.path == '/api/health':
            return httpx.Response(200, json={'capabilities': ['command-receipts']})
        result = client.post(request.url.path, json=json.loads(request.content), headers=dict(request.headers))
        if lose[0]: raise httpx.ReadTimeout('Committed but reply lost')
        return httpx.Response(result.status_code, json=result.json())
    real_client = httpx.Client
    monkeypatch.setattr(cli.httpx, 'Client', lambda **kwargs: real_client(transport=httpx.MockTransport(transport)))
    with pytest.raises((RuntimeError, httpx.ReadTimeout)):
        mcp_server.add_comment('Progress once')
    lose[0] = False
    mcp_server.add_comment('Progress once')
    assert len(service.get_task(task['id'])['comments']) == 1


def test_client_process_restart_replays_committed_request(client, tmp_path):
    import json
    import os
    import socket
    import subprocess
    import sys
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    lose = [True]
    keys = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(b'{"capabilities":["command-receipts"]}')
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            keys.append(self.headers['Idempotency-Key'])
            response = client.post('/api/tasks', json=body, headers={'Idempotency-Key': keys[-1]})
            if lose[0]:
                self.connection.shutdown(socket.SHUT_RDWR); self.connection.close(); return
            self.send_response(response.status_code); self.end_headers()
            self.wfile.write(response.content)
    server = ThreadingHTTPServer(('127.0.0.1',0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    url = f'http://127.0.0.1:{server.server_port}'
    env = {**os.environ, 'ACP_COMMAND_JOURNAL':str(tmp_path / 'commands.sqlite3')}
    code = "from acp.cli import request; import sys; print(request(sys.argv[1], 'POST', '/api/tasks', {'title':'Process restart'}))"
    def invoke():
        return subprocess.run([sys.executable,'-c',code,url],env=env,capture_output=True,text=True,timeout=20)
    try:
        first = invoke(); assert first.returncode != 0 and 'Unresolved request ID:' in first.stderr
        lose[0] = False
        second = invoke(); assert second.returncode == 0, second.stderr
        assert len(keys) == 4 and len(set(keys)) == 1
        assert len(client.get('/api/tasks').json()) == 1
    finally:
        server.shutdown(); server.server_close(); thread.join()
