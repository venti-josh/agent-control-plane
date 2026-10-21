"""The app-server runner against a fake `codex` speaking JSON-RPC over stdio (real processes, HTTP, Git)."""
import asyncio
import json
import sys

import httpx
import pytest

from acp.codex_app_server import CodexRunner, Connection, approval_question, probe
from acp.integrations import Integrations
from conftest import started
from test_integrations import comment_payload, imported
from acp.worker import Worker
from test_runners import git_repo, live_api  # noqa: F401 (fixtures)

FAKE = r'''
import json, os, sys, threading, time, urllib.request
log = open(os.environ['FAKE_CODEX_LOG'], 'a', buffering=1)
def record(**data):
    log.write(json.dumps(data) + '\n')
args = sys.argv[1:]
record(argv=args)
if 'exec' in args:  # The fallback: `codex exec --json`, reporting through the HTTP task API.
    env = {}
    for i, arg in enumerate(args):
        if arg == '-c' and args[i + 1].startswith('mcp_servers.acp.env.'):
            key, value = args[i + 1].split('=', 1)
            env[key.rsplit('.', 1)[-1]] = json.loads(value)
    sys.stdin.read()
    print(json.dumps({'type': 'thread.started', 'thread_id': 'exec-thread'}), flush=True)
    req = urllib.request.Request(env['ACP_URL'] + '/api/tasks/' + env['ACP_TASK_ID'] + '/submit-for-review',
                                 json.dumps({'summary': 'Exec result', 'run_id': env['ACP_RUN_ID']}).encode(),
                                 {'Content-Type': 'application/json'})
    urllib.request.urlopen(req, timeout=10).read()
    sys.exit(0)
sys.stderr.write('fake app-server diagnostics\n'); sys.stderr.flush()
out = threading.Lock()
replies, answers, steers = {}, {}, []
interrupted = threading.Event()
state = {}
def send(message):
    with out:
        sys.stdout.write(json.dumps(message) + '\n')
        sys.stdout.flush()
def acp(path='', data=None):
    env = state['env']
    url = env['ACP_URL'] + '/api/tasks/' + env['ACP_TASK_ID'] + path
    if data is not None:
        data = json.dumps({**data, 'run_id': env['ACP_RUN_ID']}).encode()
    req = urllib.request.Request(url, data, {'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as response:
        return json.load(response)
def ask(method, params):
    rid = 'srv-%d' % len(answers)
    answers[rid] = threading.Event()
    send({'id': rid, 'method': method, 'params': params})
    answers[rid].wait()
    return replies[rid]
def agent(thread, turn, prompt):
    def finish(status='completed'):
        send({'method': 'turn/completed', 'params': {'threadId': thread, 'turn': {'id': turn, 'items': [], 'status': status}}})
    task = acp()
    text = task['description']
    acp('/comments', {'body': 'Fake plan'})
    if '[crash]' in text:
        os._exit(3)
    if '[fail]' in text:
        send({'method': 'error', 'params': {'threadId': thread, 'turnId': turn, 'willRetry': False,
                                            'error': {'message': 'usage limit reached'}}})
        return finish('failed')
    if '[elicit]' in text:
        record(elicitation=ask('mcpServer/elicitation/request', {'threadId': thread, 'message': 'Sign in?'}))
    if '[approve]' in text or '[patch]' in text:
        if '[patch]' in text:
            send({'method': 'item/started', 'params': {'threadId': thread, 'turnId': turn, 'item': {
                'type': 'fileChange', 'id': 'patch-1', 'status': 'inProgress',
                'changes': [{'path': '/outside/notes.md', 'kind': {'type': 'add'}, 'diff': '+x'}]}}})
            reply = ask('item/fileChange/requestApproval', {'threadId': thread, 'turnId': turn,
                        'itemId': 'patch-1', 'reason': 'Write outside the workspace', 'grantRoot': None})
        else:
            reply = ask('item/commandExecution/requestApproval', {'threadId': thread, 'turnId': turn,
                        'itemId': 'cmd-1', 'command': 'touch /outside/file', 'cwd': state['cwd'],
                        'reason': 'Needs to write outside the sandbox'})
        record(decision=reply['decision'])
        if reply['decision'] == 'cancel':
            return finish('interrupted')
        time.sleep(0.3)  # Steering that came with a decline arrives during the rest of the turn.
        acp('/comments', {'body': 'Decision: ' + reply['decision'] + ' steer: ' + ' | '.join(steers)})
    if '[hang]' in text or '[steer]' in text:
        deadline = time.time() + 30
        while time.time() < deadline and not interrupted.is_set() and not ('[steer]' in text and steers):
            time.sleep(0.05)
        if interrupted.is_set():
            return
    if '[ask]' in text and not any(c['in_reply_to'] for c in task['comments']):
        acp('/request-input', {'question': 'Which output?'})
    else:
        acp('/submit-for-review', {'summary': 'Fake app-server result' + (' after ' + ' | '.join(steers) if steers else '')})
    if '[linger]' not in text:
        finish()
for line in sys.stdin:
    message = json.loads(line)
    record(message=message)
    if 'method' not in message:
        replies[message['id']] = message.get('result') or message.get('error')
        answers[message['id']].set()
        continue
    method, params, rid = message['method'], message.get('params') or {}, message.get('id')
    if method == 'initialized':
        continue
    if method == 'initialize':
        result = {'userAgent': 'fake'}
    elif method in ('thread/start', 'thread/resume'):
        state['env'] = params['config']['mcp_servers.acp.env']
        state['cwd'] = params['cwd']
        state['thread'] = params.get('threadId') or 'thread-%d' % os.getpid()
        result = {'thread': {'id': state['thread']}}
    elif method == 'thread/name/set':
        result = {}
    elif method == 'project/list' and os.environ.get('FAKE_PROJECT_ROOT'):  # Codex 0.163+
        result = {'data': [{'id': 'other', 'roots': [{'path': '/elsewhere'}]},
                           {'id': 'project-1', 'roots': [{'path': os.environ['FAKE_PROJECT_ROOT']}]}]}
    elif method == 'turn/start':
        state['turn'] = 'turn-%d' % time.time_ns()
        result = {'turn': {'id': state['turn'], 'items': [], 'status': 'inProgress'}}
        threading.Thread(target=agent, args=(params['threadId'], state['turn'], params['input'][0]['text']),
                         daemon=True).start()
    elif method == 'turn/steer':  # Reply first: the runner records delivery before the agent reports.
        send({'id': rid, 'result': {'turnId': params['expectedTurnId']}})
        time.sleep(0.2)
        steers.append(params['input'][0]['text'])
        continue
    elif method == 'turn/interrupt':
        interrupted.set()
        result = {}
        send({'id': rid, 'result': result})
        send({'method': 'turn/completed', 'params': {'threadId': params['threadId'],
              'turn': {'id': params['turnId'], 'items': [], 'status': 'interrupted'}}})
        continue
    else:
        send({'id': rid, 'error': {'code': -32601, 'message': 'unknown method ' + method}})
        continue
    send({'id': rid, 'result': result})
'''


@pytest.fixture
def fake_codex(tmp_path, monkeypatch):
    path = tmp_path / 'codex'
    path.write_text(f'#!{sys.executable}\n' + FAKE)
    path.chmod(0o755)
    log = tmp_path / 'fake-codex.jsonl'
    monkeypatch.setenv('FAKE_CODEX_LOG', str(log))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex-home'))  # No real daemon socket.

    def calls(method=None):
        rows = [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []
        if method is None:
            return rows
        return [r['message'] for r in rows if r.get('message', {}).get('method') == method]
    calls.path = str(path)
    return calls


@pytest.fixture
def svc(live_api, git_repo, fake_codex):  # noqa: F811
    live_api.settings.codex_command = fake_codex.path
    live_api.settings.default_repo = str(git_repo)
    return live_api


async def until(check, timeout=10):
    for _ in range(int(timeout / 0.05)):
        if check():
            return
        await asyncio.sleep(0.05)
    raise AssertionError('condition not reached')


async def test_runner_selection_probes_once_and_honors_run_mode(svc, fake_codex, tmp_path):
    assert await probe(fake_codex.path) is True
    assert await probe(sys.executable) is False
    assert await probe(str(tmp_path / 'missing')) is False
    runner = CodexRunner()
    for title in ('Routed', 'Routed again'):
        task = svc.create_task(title, provider='codex', state='todo')
        await Worker(svc, runners={'codex': runner}).run_once()
        assert svc.get_task(task['id'])['state'] == 'ready_for_review'
    assert runner.app_server is True and len(fake_codex('thread/start')) == 2
    probes = [m for m in fake_codex('initialize') if 'capabilities' not in m['params']]
    assert len(probes) == 2  # The direct probe of the fake above, then one for the runner's first run.
    svc.settings.codex_run_mode = 'exec'
    exec_task = svc.create_task('Forced exec', provider='codex', state='todo')
    await Worker(svc, runners={'codex': runner}).run_once()
    current = svc.get_task(exec_task['id'])
    assert current['state'] == 'ready_for_review' and current['session_id'] == 'exec-thread'
    assert any('exec' in r.get('argv', []) for r in fake_codex())


def resume_excludes_turns(fake_codex):
    return all(m['params'].get('excludeTurns') for m in fake_codex('thread/resume'))


async def test_launch_complete_then_resume_same_thread_for_answer_and_changes(svc, fake_codex, settings):
    task = svc.create_task('Ask first', '[ask] details', provider='codex', state='todo')
    worker = Worker(svc)
    await worker.run_once()
    current = svc.get_task(task['id'])
    assert current['state'] == 'waiting_on_input'
    [start] = fake_codex('thread/start')
    thread = current['session_id']
    params = start['params']
    assert params['approvalPolicy'] == 'on-request' and params['approvalsReviewer'] == 'user'
    assert params['sandbox'] == 'workspace-write' and params['cwd'] == svc.settings.default_repo
    assert params['config']['mcp_servers.acp.env']['ACP_TASK_ID'] == task['id']
    assert params['config']['sandbox_workspace_write.writable_roots'][0].endswith('.git')
    assert 'projectId' not in params  # This server has no project/list.
    config = params['config']
    assert config['mcp_servers.acp.default_tools_approval_mode'] == 'prompt'
    assert all(config[f'mcp_servers.acp.tools.{tool}.approval_mode'] == 'approve'
               for tool in ('get_task', 'add_comment', 'request_input', 'submit_for_review'))
    assert resume_excludes_turns(fake_codex)
    assert fake_codex('thread/name/set')[0]['params'] == {'threadId': thread, 'name': 'ACP: Ask first'}
    assert 'TASK SNAPSHOT' in fake_codex('turn/start')[0]['params']['input'][0]['text']
    assert current['runs'][0]['status'] == 'succeeded'
    svc.add_comment(task['id'], 'Use output A', in_reply_to=current['blocker_comment_id'], resume=True)
    await worker.run_once()
    assert svc.get_task(task['id'])['state'] == 'ready_for_review'
    [resume] = fake_codex('thread/resume')
    assert resume['params']['threadId'] == thread
    svc.request_changes(task['id'], 'Tighten it up')
    await worker.run_once()
    assert len(fake_codex('thread/resume')) == 2 and len(fake_codex('thread/start')) == 1
    assert svc.get_task(task['id'])['state'] == 'ready_for_review'
    log = (settings.data_dir / 'logs' / f"{current['runs'][0]['id']}.jsonl").read_text()
    assert 'turn/completed' in log and '"channel": "stdin"' in log and 'fake app-server diagnostics' in log


async def test_comment_on_in_progress_task_steers_the_live_turn(svc, fake_codex):
    task = svc.create_task('Steer me', '[steer]', provider='codex', state='todo')
    running = asyncio.create_task(Worker(svc).run_once())
    await until(lambda: svc.store.one('SELECT steerable FROM runs WHERE task_id=?', (task['id'],))['steerable'])
    svc.add_comment(task['id'], 'Use the blue theme')
    await asyncio.wait_for(running, 15)
    current = svc.get_task(task['id'])
    assert current['state'] == 'ready_for_review' and len(current['runs']) == 1
    [steer] = fake_codex('turn/steer')
    assert 'Use the blue theme' in steer['params']['input'][0]['text']
    assert steer['params']['expectedTurnId'].startswith('turn-')
    assert 'Use the blue theme' in current['comments'][-1]['body']
    assert svc.pending_inputs(current['runs'][0]['id']) == []


async def test_stop_interrupts_the_turn(svc, fake_codex):
    task = svc.create_task('Stop me', '[hang]', provider='codex', state='todo')
    running = asyncio.create_task(Worker(svc).run_once())
    await until(lambda: svc.get_task(task['id'])['state'] == 'in_progress')
    await until(lambda: fake_codex('turn/start'))
    svc.cancel(task['id'])
    await asyncio.wait_for(running, 15)
    assert fake_codex('turn/interrupt')
    current = svc.get_task(task['id'])
    assert current['state'] == 'planning' and current['runs'][0]['status'] == 'cancelled'


@pytest.mark.parametrize('answer,decision', [('accept', 'accept'), ('Accept for session.', 'acceptForSession'),
                                             ('decline', 'decline')])
async def test_command_approval_is_answered_from_the_board(svc, fake_codex, answer, decision):
    task = svc.create_task('Approve me', '[approve]', provider='codex', state='todo')
    running = asyncio.create_task(Worker(svc).run_once())
    await until(lambda: svc.get_task(task['id'])['state'] == 'waiting_on_input')
    current = svc.get_task(task['id'])
    blocker = next(c for c in current['comments'] if c['id'] == current['blocker_comment_id'])
    assert 'touch /outside/file' in blocker['body'] and 'Needs to write outside the sandbox' in blocker['body']
    assert current['approval']['decisions'] == ['accept', 'accept for session', 'decline']
    assert current['runs'][0]['status'] == 'running'
    result = svc.add_comment(task['id'], answer, in_reply_to=blocker['id'], resume=True)
    assert result['resume_queued'] is True
    await asyncio.wait_for(running, 15)
    current = svc.get_task(task['id'])
    assert [r['decision'] for r in fake_codex() if 'decision' in r] == [decision]
    assert len(current['runs']) == 1 and current['state'] == 'ready_for_review'
    assert current['approval'] is None


async def test_other_answer_declines_and_steers_with_the_text(svc, fake_codex):
    task = svc.create_task('Approve me', '[approve]', provider='codex', state='todo')
    running = asyncio.create_task(Worker(svc).run_once())
    await until(lambda: svc.get_task(task['id'])['state'] == 'waiting_on_input')
    svc.add_comment(task['id'], 'No, write it inside the repo instead', resume=True,
                    in_reply_to=svc.get_task(task['id'])['blocker_comment_id'])
    await asyncio.wait_for(running, 15)
    assert [r['decision'] for r in fake_codex() if 'decision' in r] == ['decline']
    [steer] = fake_codex('turn/steer')
    assert 'write it inside the repo instead' in steer['params']['input'][0]['text']
    assert any(c['body'].startswith('Decision: decline steer:') and 'inside the repo' in c['body']
               for c in svc.get_task(task['id'])['comments'])


async def test_file_change_approval_lists_files(svc, fake_codex):
    task = svc.create_task('Patch', '[patch]', provider='codex', state='todo')
    running = asyncio.create_task(Worker(svc).run_once())
    await until(lambda: svc.get_task(task['id'])['state'] == 'waiting_on_input')
    current = svc.get_task(task['id'])
    blocker = next(c for c in current['comments'] if c['id'] == current['blocker_comment_id'])
    assert '/outside/notes.md' in blocker['body'] and current['approval']['method'] == 'item/fileChange/requestApproval'
    await asyncio.sleep(3.5)  # Past the 3 s terminal-state rule: a parked approval keeps the run alive.
    assert not running.done() and svc.get_task(task['id'])['runs'][0]['status'] == 'running'
    svc.add_comment(task['id'], 'accept', in_reply_to=blocker['id'], resume=True)
    await asyncio.wait_for(running, 15)
    assert svc.get_task(task['id'])['state'] == 'ready_for_review'


async def test_run_timeout_while_waiting_cancels_the_request(svc, fake_codex):
    svc.settings.run_timeout_seconds = 1.5
    task = svc.create_task('Never answered', '[approve]', provider='codex', state='todo')
    await Worker(svc).run_once()
    current = svc.get_task(task['id'])
    assert [r['decision'] for r in fake_codex() if 'decision' in r] == ['cancel']
    assert current['runs'][0]['status'] == 'failed' and 'run limit' in current['runs'][0]['error']
    assert current['approval'] is None
    # A late answer is ordinary input for a new run, never a reply to the dead request.
    svc.add_comment(task['id'], 'accept', in_reply_to=current['blocker_comment_id'], resume=True)
    assert svc.claim_next()['inputs'][0]['trigger'] == 'input'


@pytest.mark.parametrize('mode,policy,reviewer', [('auto_review', 'on-request', 'auto_review'),
                                                  ('never', 'never', 'user')])
async def test_approval_mode_is_passed_to_codex(svc, fake_codex, mode, policy, reviewer):
    svc.settings.codex_approvals = mode
    svc.create_task('Modes', provider='codex', state='todo')
    await Worker(svc).run_once()
    [start] = fake_codex('thread/start')
    assert (start['params']['approvalPolicy'], start['params']['approvalsReviewer']) == (policy, reviewer)


async def test_server_crash_fails_the_run(svc, fake_codex):
    task = svc.create_task('Crash', '[crash]', provider='codex', state='todo')
    await Worker(svc).run_once()
    current = svc.get_task(task['id'])
    assert current['runs'][0]['status'] == 'failed'
    assert 'exited during the turn' in current['runs'][0]['error']
    assert current['state'] == 'waiting_on_input'


async def test_lingering_turn_is_interrupted_after_the_task_reports(svc, fake_codex):
    task = svc.create_task('Linger', '[linger]', provider='codex', state='todo')
    started_at = asyncio.get_running_loop().time()
    await asyncio.wait_for(Worker(svc).run_once(), 15)
    assert asyncio.get_running_loop().time() - started_at > 3
    assert fake_codex('turn/interrupt')
    assert svc.get_task(task['id'])['runs'][0]['status'] == 'succeeded'


def test_approval_question_shows_command_directory_and_reason():
    text = approval_question({'method': 'item/commandExecution/requestApproval', 'params': {
        'command': 'curl https://x', 'cwd': '/repo', 'reason': 'Needs network'}}, {})
    assert '```\ncurl https://x\n```' in text and '`/repo`' in text and 'Needs network' in text
    assert '`accept for session`' in text


def test_dispatcher_bound_sessions_reset_when_the_old_tables_are_dropped(tmp_path):
    import sqlite3
    from acp.store import Store
    path = tmp_path / 'board.sqlite3'
    Store(path)
    db = sqlite3.connect(path)
    db.executescript("""
        CREATE TABLE desktop_runs (run_id TEXT PRIMARY KEY, phase TEXT, thread_id TEXT);
        CREATE TABLE desktop_inputs (comment_id TEXT PRIMARY KEY, run_id TEXT, status TEXT);
        INSERT INTO metadata VALUES('codex_dispatcher', 'chat');
        INSERT INTO tasks(id,title,state,provider,session_id,created_at,updated_at)
            VALUES('app','App chat','waiting_on_input','codex','app-chat',0,0),
                  ('cli','CLI thread','waiting_on_input','codex','exec-thread',0,0);
        INSERT INTO runs(id,task_id,status,trigger,inputs,created_at) VALUES('r','app','succeeded','todo','[]',0);
        INSERT INTO desktop_runs VALUES('r','complete','app-chat');
    """)
    db.commit()
    db.close()
    store = Store(path)
    assert {t['id']: t['session_id'] for t in store.all('SELECT * FROM tasks')} == {'app': None, 'cli': 'exec-thread'}
    tables = {r['name'] for r in store.all("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not {'desktop_runs', 'desktop_inputs'} & tables
    assert store.one("SELECT 1 FROM metadata WHERE key='codex_dispatcher'") is None


async def test_thread_joins_the_codex_app_project_containing_the_repo(svc, fake_codex, monkeypatch, git_repo):  # noqa: F811
    monkeypatch.setenv('FAKE_PROJECT_ROOT', str(git_repo.parent))
    svc.create_task('In a project', provider='codex', state='todo')
    await Worker(svc).run_once()
    [start] = fake_codex('thread/start')
    assert start['params']['projectId'] == 'project-1'
    assert fake_codex('initialize')[-1]['params']['capabilities'] == {'experimentalApi': True}


def parked(service, **kwargs):
    """A live app-server run waiting on a command approval, as the runner leaves it."""
    task, run = started(service, provider='codex', **kwargs)
    service.mark_steerable(run['id'])
    approval = service.request_approval(run['id'], {'id': 0, 'method': 'item/commandExecution/requestApproval',
                                                    'params': {}}, 'Codex asks to run a command')
    return service.get_task(task['id']), run, approval


def test_answer_resumes_the_same_run_and_its_activity(service):
    task, run, approval = parked(service)
    assert task['state'] == 'waiting_on_input'
    assert service.store.one('SELECT activity_finished_at FROM runs WHERE id=?', (run['id'],))['activity_finished_at']
    service.add_comment(task['id'], 'accept', in_reply_to=task['blocker_comment_id'], resume=True)
    assert service.get_task(task['id'])['state'] == 'in_progress' and service.claim_next() is None
    assert service.store.one('SELECT activity_finished_at FROM runs WHERE id=?', (run['id'],))['activity_finished_at'] is None
    assert [a['decision'] for a in service.answered_approvals(run['id'])] == ['accept']


async def test_linear_reply_answers_the_approval(linear_service):
    task = await imported(linear_service)
    linear_service.update_task(task['id'], provider='codex')
    linear_service.move_task(task['id'], 'todo')
    run = linear_service.claim_next()
    linear_service.mark_started(run['id'])
    linear_service.request_approval(run['id'], {'id': 0, 'method': 'item/commandExecution/requestApproval',
                                                'params': {}}, 'Codex asks to run a command')
    async with httpx.AsyncClient() as client:
        await Integrations(linear_service, client).handle_linear(comment_payload('Accept'))
    assert [a['decision'] for a in linear_service.answered_approvals(run['id'])] == ['accept']
    assert linear_service.get_task(task['id'])['state'] == 'in_progress'
    assert linear_service.claim_next() is None


def test_approval_answered_by_another_client_resumes_the_task(service):
    task, run, approval = parked(service)
    service.approval_resolved_elsewhere(approval)
    current = service.get_task(task['id'])
    assert current['state'] == 'in_progress' and current['approval'] is None
    assert current['comments'][-1]['body'] == 'Answered outside ACP.'
    service.submit_for_review(task['id'], 'Done', run_id=run['id'])


def test_steering_that_cannot_be_delivered_becomes_a_followup(service):
    task, run = started(service, provider='codex')
    service.mark_steerable(run['id'])
    comment = service.add_comment(task['id'], 'Late requirement')
    service.defer_input(comment['id'])
    service.submit_for_review(task['id'], 'Done', run_id=run['id'])  # No longer blocked.
    service.finish_run(run['id'])
    assert service.claim_next()['inputs'] == [{'trigger': 'comment', 'comment_id': comment['id']}]


async def test_unanswered_request_times_out_with_a_message(tmp_path):
    class Silent:
        process = None
        async def send(self, text): pass
        async def recv(self): await asyncio.sleep(10)
        async def close(self): pass
    with (tmp_path / 'log.jsonl').open('w') as log:
        rpc = Connection(Silent(), log)
        with pytest.raises(TimeoutError, match='did not answer thread/start'):
            await rpc.request('thread/start', timeout=0.05)
        await rpc.close()


async def test_failed_turn_reports_codex_error(svc, fake_codex):
    task = svc.create_task('Fails', '[fail]', provider='codex', state='todo')
    await Worker(svc).run_once()
    current = svc.get_task(task['id'])
    assert current['runs'][0]['status'] == 'failed' and 'usage limit reached' in current['runs'][0]['error']


async def test_other_server_requests_get_an_error_so_the_turn_continues(svc, fake_codex):
    task = svc.create_task('Elicit', '[elicit]', provider='codex', state='todo')
    await asyncio.wait_for(Worker(svc).run_once(), 15)
    [reply] = [r['elicitation'] for r in fake_codex() if 'elicitation' in r]
    assert 'does not handle' in reply['message']
    assert svc.get_task(task['id'])['state'] == 'ready_for_review'


async def test_control_socket_websocket_roundtrip_and_stale_socket_fallback(svc, fake_codex, monkeypatch):
    import base64
    import hashlib
    import shutil
    import struct
    import tempfile
    from pathlib import Path
    from acp.codex_app_server import UnixWebSocket, control_socket
    short = Path(tempfile.mkdtemp(prefix='acp-ws-', dir='/tmp'))  # Unix socket paths are length-limited.
    monkeypatch.setenv('CODEX_HOME', str(short / 'home'))

    async def serve(reader, writer):  # A minimal WebSocket echo server, like the daemon's framing.
        head = await reader.readuntil(b'\r\n\r\n')
        key = [l.split(b': ')[1] for l in head.split(b'\r\n') if l.lower().startswith(b'sec-websocket-key')][0]
        accept = base64.b64encode(hashlib.sha1(key + b'258EAFA5-E914-47DA-95CA-C5AB0DC85B11').digest())
        writer.write(b'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
                     b'Sec-WebSocket-Accept: ' + accept + b'\r\n\r\n')
        writer.write(b'\x89\x00')  # A ping the client must answer.
        while True:
            first, second = await reader.readexactly(2)
            n = second & 0x7F
            n = struct.unpack('!H', await reader.readexactly(2))[0] if n == 126 else \
                struct.unpack('!Q', await reader.readexactly(8))[0] if n == 127 else n
            mask = await reader.readexactly(4)
            data = bytes(b ^ mask[i % 4] for i, b in enumerate(await reader.readexactly(n)))
            if first & 0x0F == 0xA:
                continue
            size = bytes([n]) if n < 126 else b'\x7e' + struct.pack('!H', n) if n < 65536 else b'\x7f' + struct.pack('!Q', n)
            writer.write(b'\x81' + size + data)
            await writer.drain()

    path = short / 'ws.sock'
    server = await asyncio.start_unix_server(serve, str(path))
    socket = await UnixWebSocket.connect(path)
    for size in (5, 300, 70000):  # 7-bit, 16-bit and 64-bit lengths.
        await socket.send('x' * size)
        assert await socket.recv() == 'x' * size
    await socket.close()
    server.close()

    async def hang_up(reader, writer):
        writer.close()
    control_socket().parent.mkdir(parents=True)
    stale = await asyncio.start_unix_server(hang_up, str(control_socket()))
    task = svc.create_task('Stale socket', provider='codex', state='todo')
    await Worker(svc).run_once()
    stale.close()
    shutil.rmtree(short)
    assert svc.get_task(task['id'])['state'] == 'ready_for_review' and fake_codex('thread/start')
