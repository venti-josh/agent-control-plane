"""Real processes + real HTTP + real Git, using fake provider executables.

These exercise our adapter, not the provider's executable or paid model API.
"""
import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import httpx
import pytest
import uvicorn

from acp.api import create_app
from acp.runners import CLIRunner, build_command, parse_event, launch_directory, stop_process
from acp.worker import Worker


@pytest.fixture
def git_repo(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    for cmd in [ ['init', '-b', 'main'], ['config', 'user.name', 'Test'],
                ['config', 'user.email', 'test@example.com'] ]:
        subprocess.run(['git', *cmd], cwd=repo, check=True, capture_output=True)
    (repo / 'README.md').write_text('Initial content\n')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-m', 'initial'], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def live_api(settings):
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    settings.base_url = f'http://127.0.0.1:{sock.getsockname()[1]}'
    app = create_app(settings, start_worker=False)
    server = uvicorn.Server(uvicorn.Config(app, log_level='error', ws='none'))
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    for _ in range(150):
        if server.started:
            break
        time.sleep(.02)
    assert server.started
    yield app.state.service
    server.should_exit = True
    thread.join(timeout=5)
    assert not thread.is_alive()
    sock.close()


@pytest.fixture
def fake_cli(tmp_path):
    path = tmp_path / 'fake-agent'
    path.write_text(f'#!{sys.executable}\n' + r'''
import json, os, sys, time, subprocess
from pathlib import Path
import urllib.request
args = sys.argv[1:]
claude = '--mcp-config' in args
if claude:
    conf = json.loads(args[args.index('--mcp-config')+1])['mcpServers']['acp']
    env = conf['env']
    session = args[args.index('--resume')+1] if '--resume' in args else 'claude-retained-session'
else:
    env = {}
    for i, arg in enumerate(args):
        if arg == '-c' and args[i+1].startswith('mcp_servers.acp.env.'):
            key, val = args[i+1].split('=', 1)
            env[key.rsplit('.', 1)[-1]] = json.loads(val)
    session = args[args.index('resume')+1] if 'resume' in args else 'codex-retained-session'
prompt = sys.stdin.read()
assert 'TASK SNAPSHOT' in prompt
url = env['ACP_URL'] + '/api/tasks/' + env['ACP_TASK_ID']
run_id = env['ACP_RUN_ID']
def request(path='', data=None):
    req = urllib.request.Request(url+path, data=None if data is None else json.dumps(data).encode(),
                                  headers={'Content-Type': 'application/json'})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)
print(json.dumps({'type':'system','session_id':session} if claude else
                 {'type':'thread.started','thread_id':session}), flush=True)
if '[subagents]' in prompt:
    if claude:
        child_event = {'type':'system', 'subtype':'task_started', 'task_id':'child', 'task_type':'local_agent'}
    else:
        child_event = {'type':'item.completed', 'item': {'type':'collab_tool_call', 'status':'completed',
                       'agents_states': {'child': {'status':'running'}}}}
    print(json.dumps(child_event), flush=True)
    time.sleep(.4)
launch_cwd = os.getcwd()
chosen = Path(launch_cwd).parent / 'provider-workspace'
if not chosen.exists():
    subprocess.run(['git', 'worktree', 'add', '-b', 'provider-owned', str(chosen)], check=True, capture_output=True)
os.chdir(chosen)
Path('fake-calls.jsonl').open('a').write(json.dumps({'args':args,'cwd':launch_cwd,'chosen':os.getcwd()})+'\n')
task = request()
if '[hang]' in task['description']:
    time.sleep(90)
if '[error]' in task['description']:
    print(json.dumps({'type':'result','is_error':True,'result':'test failure'} if claude else
                     {'type':'turn.failed','error':{'message':'test failure'}}), flush=True)
    sys.exit(1)
request('/comments', {'body':'Fake provider plan', 'run_id':run_id})
if '[ask]' in task['description'] and not any(c['in_reply_to'] for c in task['comments']):
    request('/request-input', {'question':'Which output?', 'run_id':run_id})
else:
    Path('output.txt').write_text('Simulated provider output\n')
    request('/submit-for-review', {'summary':'Fake provider complete', 'run_id':run_id})
''')
    path.chmod(0o755)
    return str(path)


@pytest.mark.parametrize('provider', ['claude', 'codex'])
async def test_cli_stdout_subagent_activity_reaches_api_and_closes_with_parent(
        live_api, git_repo, fake_cli, provider):
    svc = live_api
    svc.settings.claude_command = svc.settings.codex_command = fake_cli
    task = svc.create_task('Observed child', description='[subagents]', provider=provider,
                           state='todo', repo_path=str(git_repo))
    pending = asyncio.create_task(Worker(svc).run_once())
    try:
        async with httpx.AsyncClient(base_url=svc.settings.base_url) as api:
            for _ in range(300):
                data = (await api.get('/api/activity')).json()
                if data['subagents']:
                    break
                await asyncio.sleep(.01)
            assert data['running'] == 2 and data['subagents'] == 1
            await pending
            data = (await api.get('/api/activity')).json()
            assert data['running'] == 0 and data['subagents'] == 0
            assert data['averages']['30m'] > 0
            intervals = svc.store.all('SELECT * FROM subagent_activity WHERE run_id IN '
                                      '(SELECT id FROM runs WHERE task_id=?)', (task['id'],))
            assert len(intervals) == 1 and intervals[0]['finished_at'] is not None
    finally:
        if not pending.done():
            pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.parametrize('provider', ['claude','codex'])
async def test_real_process_input_and_review_loop(live_api, git_repo, fake_cli, provider):
    svc = live_api
    svc.settings.claude_command = svc.settings.codex_command = fake_cli
    task = svc.create_task('Subprocess integration', description='[ask]', provider=provider,
                           state='todo', repo_path=str(git_repo))
    worker = Worker(svc)
    assert await worker.run_once()
    task = svc.get_task(task['id'])
    assert task['state'] == 'waiting_on_input'
    assert task['session_id'] == f'{provider}-retained-session'
    assert task['runs'][0]['status'] == 'succeeded'
    workspace = git_repo.parent / 'provider-workspace'
    assert task['workspace'] is None and task['branch'] is None
    assert not (svc.settings.data_dir / 'worktrees').exists()
    svc.add_comment(task['id'], 'Use a text file', in_reply_to=task['blocker_comment_id'], resume=True)
    assert await worker.run_once()
    task = svc.get_task(task['id'])
    assert task['state'] == 'ready_for_review'
    assert task['workspace'] is None
    assert (Path(workspace) / 'output.txt').is_file()
    assert not (git_repo / 'output.txt').exists()
    calls = [json.loads(line) for line in (Path(workspace)/'fake-calls.jsonl').read_text().splitlines()]
    assert len(calls) == 2
    assert calls[0]['cwd'] == calls[1]['cwd'] == str(git_repo)
    assert calls[0]['chosen'] == calls[1]['chosen'] == str(workspace)
    assert task['session_id'] in calls[1]['args']
    svc.request_changes(task['id'], 'Improve the output')
    await worker.run_once()
    assert svc.get_task(task['id'])['state'] == 'ready_for_review'
    assert len(list((svc.settings.data_dir / 'logs').glob('*.jsonl'))) == 3


@pytest.mark.parametrize('provider', ['claude', 'codex'])
async def test_real_process_error_is_blocked(live_api, git_repo, fake_cli, provider):
    svc = live_api
    svc.settings.claude_command = svc.settings.codex_command = fake_cli
    task = svc.create_task('Fail', description='[error]', provider=provider, state='todo', repo_path=str(git_repo))
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    assert task['state'] == 'waiting_on_input'
    assert 'test failure' in task['last_error']
    assert task['runs'][0]['status'] == 'failed'


async def test_real_process_timeout_reaped(live_api, git_repo, fake_cli):
    svc = live_api
    svc.settings.claude_command = fake_cli
    svc.settings.run_timeout_seconds = .4
    task = svc.create_task('Timeout', description='[hang]', provider='claude', state='todo', repo_path=str(git_repo))
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    assert task['state'] == 'waiting_on_input'
    assert 'run limit' in task['last_error']
    assert task['runs'][0]['status'] == 'failed'


async def test_real_process_cancellation_reaped(live_api, git_repo, fake_cli):
    svc = live_api
    svc.settings.claude_command = fake_cli
    task = svc.create_task('Cancel', description='[hang]', provider='claude', state='todo', repo_path=str(git_repo))
    running = asyncio.create_task(Worker(svc).run_once())
    for _ in range(100):
        if svc.get_task(task['id'])['state'] == 'in_progress':
            break
        await asyncio.sleep(.03)
    svc.cancel(task['id'])
    await asyncio.wait_for(running, 5)
    task = svc.get_task(task['id'])
    assert task['state'] == 'planning'
    assert task['runs'][0]['status'] == 'cancelled'


async def test_cleanup_is_safe_to_repeat_after_group_exits():
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-c', 'import time; time.sleep(90)', start_new_session=True)
    try:
        await stop_process(process)
        await stop_process(process)
        assert process.returncode is not None
    finally:
        if process.returncode is None:
            os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


async def test_cleanup_accepts_macos_eperm_only_for_absent_group(monkeypatch):
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-c', 'pass', start_new_session=True)
    await process.wait()

    def macos_absent_group(pgid, sig):
        raise PermissionError('macOS reports EPERM for a vanished group')

    monkeypatch.setattr(os, 'killpg', macos_absent_group)
    await stop_process(process)
    assert process.returncode == 0


async def test_cleanup_kills_descendants_after_leader_exits(tmp_path):
    ready = tmp_path / 'child.pid'
    child_code = (
        'import os, signal, time; from pathlib import Path; '
        'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
        f'Path({str(ready)!r}).write_text(str(os.getpid())); time.sleep(90)'
    )
    parent_code = (
        'import subprocess, sys, time; '
        f'subprocess.Popen([sys.executable, "-c", {child_code!r}]); time.sleep(90)'
    )
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-c', parent_code, start_new_session=True)
    try:
        for _ in range(100):
            if ready.exists():
                break
            await asyncio.sleep(.03)
        assert ready.exists(), 'descendant did not start'
        await stop_process(process)
        for _ in range(100):
            result = subprocess.run(['ps', '-p', ready.read_text(), '-o', 'stat='],
                                    capture_output=True, text=True, check=False)
            if not result.stdout.strip() or result.stdout.strip().startswith('Z'):
                break
            await asyncio.sleep(.03)
        else:
            pytest.fail('live descendant survived cleanup')
    finally:
        # Repeat cleanup also covers the already-exited leader case.
        await stop_process(process)


async def test_cleanup_preserves_permission_errors_for_existing_group(monkeypatch):
    process = await asyncio.create_subprocess_exec(
        sys.executable, '-c', 'import time; time.sleep(90)', start_new_session=True)
    real_killpg = os.killpg

    def denied(pgid, sig):
        raise PermissionError('permission denied for a live process group')

    try:
        monkeypatch.setattr(os, 'killpg', denied)
        with pytest.raises(PermissionError, match='live process group'):
            await stop_process(process)
    finally:
        real_killpg(process.pid, signal.SIGKILL)
        await process.wait()


async def test_missing_executable_explains_failure(service):
    service.settings.codex_command = '/no/such/executable'
    task = service.create_task('Missing', provider='codex', state='todo')
    await Worker(service).run_once()
    task = service.get_task(task['id'])
    assert task['state'] == 'waiting_on_input'
    assert 'not installed' in task['last_error']


async def test_retained_worktree_not_silently_recreated(service, git_repo):
    task = service.create_task('Worktree', repo_path=str(git_repo))
    path = git_repo.parent / 'legacy-worktree'
    subprocess.run(['git', 'worktree', 'add', '-b', 'legacy', str(path)], cwd=git_repo,
                   check=True, capture_output=True)
    with service.store.transaction() as db:
        db.execute('UPDATE tasks SET workspace=?,branch=? WHERE id=?', (str(path), 'legacy', task['id']))
    task = service.get_task(task['id'])
    assert launch_directory(task) == path
    path.rename(path.with_name('moved'))
    with pytest.raises(RuntimeError, match='missing'):
        launch_directory(task)


async def test_new_task_does_not_create_or_record_a_workspace(service, git_repo):
    task = service.create_task('Agent chooses workspace', provider='codex', repo_path=str(git_repo))
    before = subprocess.check_output(['git', 'worktree', 'list', '--porcelain'], cwd=git_repo)
    assert launch_directory(task) == git_repo
    assert subprocess.check_output(['git', 'worktree', 'list', '--porcelain'], cwd=git_repo) == before
    task = service.get_task(task['id'])
    assert task['workspace'] is None and task['branch'] is None
    assert not (service.settings.data_dir / 'worktrees').exists()


def test_explicit_sessions_and_native_mcp_config(settings):
    claude = build_command(settings, 'claude', 'task', 'run', 'specific-session')
    assert '--resume' in claude and 'specific-session' in claude
    config = json.loads(claude[claude.index('--mcp-config')+1])
    assert config['mcpServers']['acp']['env']['ACP_RUN_ID'] == 'run'
    codex = build_command(settings, 'codex', 'task', 'run', 'specific-thread', '/repo/.git')
    assert ['exec', 'resume', 'specific-thread', '--json', '-'] == codex[-5:]
    assert '--last' not in codex
    assert 'sandbox_mode="workspace-write"' in codex
    assert 'mcp_servers.acp.required=true' in codex
    assert 'sandbox_workspace_write.writable_roots=["/repo/.git"]' in codex
    assert parse_event('claude', {'type':'system','session_id':'c'}) == ('c',None)
    assert parse_event('codex', {'type':'thread.started','thread_id':'d'}) == ('d',None)


@pytest.mark.parametrize('session', [None, 'retained-thread'])
def test_codex_headless_task_tools_have_explicit_scoped_approval(settings, session):
    command = build_command(settings, 'codex', 'task', 'run', session)
    config = tomllib.loads('\n'.join(command[i + 1] for i, arg in enumerate(command) if arg == '-c'))
    server = config['mcp_servers']['acp']
    assert set(server['enabled_tools']) == {'get_task', 'add_comment', 'request_input', 'submit_for_review'}
    assert server['default_tools_approval_mode'] == 'prompt'
    assert {tool: values['approval_mode'] for tool, values in server['tools'].items()} == {
        'get_task': 'approve', 'add_comment': 'approve',
        'request_input': 'approve', 'submit_for_review': 'approve',
    }
    assert config['sandbox_mode'] == 'workspace-write'
    assert config['approval_policy'] == 'never'


def test_guard_exits_when_parent_pipe_closes(tmp_path):
    read_fd, write_fd = os.pipe()
    proc = subprocess.Popen([sys.executable, '-m', 'acp.process_guard', str(read_fd),
                             sys.executable, '-c', 'import time; time.sleep(90)'],
                            pass_fds=(read_fd,), start_new_session=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    os.close(read_fd)
    try:
        first = json.loads(proc.stdout.readline())
        assert first['type'] == 'acp.process_started'
        os.close(write_fd)
        write_fd = -1
        assert proc.wait(timeout=5) == -signal.SIGKILL
        # Both processes are in the killed group; Linux may briefly retain the child as a zombie.
        status = Path(f'/proc/{first["pid"]}/status')
        if status.exists():
            assert 'Z (zombie)' in status.read_text()
    finally:
        if write_fd != -1:
            os.close(write_fd)
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate(timeout=5)


async def test_native_http_sse_replay(live_api):
    svc = live_api
    task = svc.create_task('SSE replay')
    previous = svc.store.events()[-1]['id']
    svc.add_comment(task['id'], 'A new event')
    expected = svc.store.events()[-1]
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        async with client.stream('GET', svc.settings.base_url + '/api/events/stream',
                                 headers={'Last-Event-ID': str(previous)}) as response:
            assert response.headers['content-type'].startswith('text/event-stream')
            lines = []
            async for line in response.aiter_lines():
                if line == '' and lines:
                    break
                lines.append(line)
    assert f'id: {expected["id"]}' in lines
    assert 'event: board' in lines
    data = json.loads(next(line[6:] for line in lines if line.startswith('data: ')))
    assert data['type'] == 'comment.created'
    assert data['task_id'] == task['id']


async def test_task_repository_cannot_replace_installed_process_guard(live_api, git_repo, fake_cli):
    shadow = git_repo / 'acp'
    shadow.mkdir()
    (shadow / '__init__.py').write_text('raise RuntimeError("Repository code replaced ACP")\n')
    subprocess.run(['git', 'add', '.'], cwd=git_repo, check=True)
    subprocess.run(['git', 'commit', '-m', 'Add task-owned acp package'],
                   cwd=git_repo, check=True, capture_output=True)
    svc = live_api
    svc.settings.codex_command = fake_cli
    task = svc.create_task('Isolated installed guard', provider='codex',
                           state='todo', repo_path=str(git_repo))
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    assert task['state'] == 'ready_for_review', task['last_error']


def test_permission_denied_for_zombie_only_group_is_already_stopped(monkeypatch):
    from acp.runners import signal_group
    # Keep an exited session leader unreaped so ps can see the zombie group.
    helper = subprocess.Popen([sys.executable, '-c', '''
import os, sys
pid = os.fork()
if pid == 0:
    os.setsid()
    os._exit(0)
print(pid, flush=True)
sys.stdin.readline()
os.waitpid(pid, 0)
'''], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        pid = int(helper.stdout.readline())
        for _ in range(100):
            state = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='],
                                   capture_output=True, text=True, check=True).stdout.strip()
            if state.startswith('Z'):
                break
            time.sleep(.01)
        assert state.startswith('Z')
        def denied(pgid, sig):
            raise PermissionError('macOS rejects signals to an exited zombie group')
        monkeypatch.setattr(os, 'killpg', denied)
        signal_group(pid, signal.SIGKILL)
    finally:
        helper.communicate('\n', timeout=5)
