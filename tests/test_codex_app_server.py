"""The app-server runner against a fake `codex` speaking JSON-RPC over stdio (real processes, HTTP, Git)."""
import asyncio
import json
import sys

import pytest

from acp.codex_app_server import CodexRunner, approval_question, probe
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
        replies[message['id']] = message.get('result') or {}
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
    elif method == 'turn/steer':
        steers.append(params['input'][0]['text'])
        result = {'turnId': params['expectedTurnId']}
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
    task = svc.create_task('Routed', provider='codex', state='todo')
    await Worker(svc, runners={'codex': runner}).run_once()
    assert runner.app_server is True
    assert fake_codex('thread/start') and svc.get_task(task['id'])['state'] == 'ready_for_review'
    svc.settings.codex_run_mode = 'exec'
    exec_task = svc.create_task('Forced exec', provider='codex', state='todo')
    await Worker(svc, runners={'codex': runner}).run_once()
    current = svc.get_task(exec_task['id'])
    assert current['state'] == 'ready_for_review' and current['session_id'] == 'exec-thread'
    assert any('exec' in r.get('argv', []) for r in fake_codex())


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
    assert 'turn/completed' in log and '"channel": "stdin"' in log


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
    assert 'Decision: decline steer:' in svc.get_task(task['id'])['comments'][-2]['body']


async def test_file_change_approval_lists_files(svc, fake_codex):
    task = svc.create_task('Patch', '[patch]', provider='codex', state='todo')
    running = asyncio.create_task(Worker(svc).run_once())
    await until(lambda: svc.get_task(task['id'])['state'] == 'waiting_on_input')
    current = svc.get_task(task['id'])
    blocker = next(c for c in current['comments'] if c['id'] == current['blocker_comment_id'])
    assert '/outside/notes.md' in blocker['body'] and current['approval']['method'] == 'item/fileChange/requestApproval'
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
    await asyncio.wait_for(Worker(svc).run_once(), 15)
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
