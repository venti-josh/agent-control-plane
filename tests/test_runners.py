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
from acp import runners
from acp.config import Settings
from acp.runners import (CLIRunner, background_args, build_command, claude, parse_event, launch_directory,
                         stop_process)
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
if args == ['--help']:
    print('Usage: fake-agent [options] (no background sessions)')
    sys.exit(0)
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


@pytest.fixture
def fake_bg_cli(tmp_path, monkeypatch):
    """A `claude` that emulates --bg, agents --json and stop with a state directory.

    Each launch spawns a detached worker that reports through the ACP HTTP API the way
    acp.mcp_server does, then stays alive idle until `stop`, as a real session would.
    """
    monkeypatch.setattr(runners, 'SETTLE_SECONDS', 0.3)  # The fake session never exits on its own.
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'claude-home'))  # Where Claude keeps job records.
    state = tmp_path / 'claude-state'
    state.mkdir()
    path = tmp_path / 'fake-claude'
    path.write_text(f'#!{sys.executable}\nSTATE = {str(state)!r}\n' + r"""
import json, os, signal, subprocess, sys, time, uuid
from pathlib import Path
import urllib.request
args = sys.argv[1:]
state = Path(STATE)

def load(short):
    return json.loads((state / f'{short}.json').read_text())

def save(entry):
    tmp = state / f'{entry["id"]}.json.tmp'
    tmp.write_text(json.dumps(entry))
    os.replace(tmp, state / f'{entry["id"]}.json')

def entries():
    found = []
    for file in sorted(state.glob('*.json')):
        entry = load(file.stem)
        if entry.get('failAt') and time.time() >= entry['failAt']:
            del entry['failAt']; entry['state'] = 'failed'
            if entry.pop('record', False):
                job = Path(os.environ['CLAUDE_CONFIG_DIR']) / 'jobs' / entry['id']
                job.mkdir(parents=True, exist_ok=True)
                (job / 'state.json').write_text(json.dumps({'state': 'failed', 'detail': 'exit 1 before init'}))
            save(entry)
        if entry.get('pid'):
            try:
                os.kill(entry['pid'], 0)
            except ProcessLookupError:
                entry.pop('pid'); entry.pop('status', None); entry['state'] = 'exited'
                save(entry)
        found.append(entry)
    return found

if args == ['--help']:
    print('Usage: claude [options] [command] [prompt]\n  --bg, --background  Start the session in the background')
    sys.exit(0)
if args[:1] == ['agents']:
    print(json.dumps([e for e in entries() if '--all' in args or e.get('pid')]))
    sys.exit(0)
if args[:1] == ['stop']:
    entry = next((e for e in entries() if e['id'] == args[1]), None)
    if not entry or not entry.get('pid'):
        print(f'no running background session {args[1]}', file=sys.stderr)
        sys.exit(1)
    if (state / 'stop-fails').exists():
        print('background service unreachable', file=sys.stderr)
        sys.exit(1)
    os.kill(entry['pid'], signal.SIGKILL)
    entry.pop('pid'); entry.pop('status', None); entry['state'] = 'stopped'
    save(entry)
    print(f'stopped {args[1]}')
    sys.exit(0)
if args[:1] == ['worker']:
    entry = load(args[1])
    env = entry['mcp']
    url = env['ACP_URL'] + '/api/tasks/' + env['ACP_TASK_ID']
    def request(path='', data=None):
        req = urllib.request.Request(url + path, data=None if data is None else json.dumps(data).encode(),
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=10) as r:
            return json.load(r)
    def status(value, state):
        current = load(args[1]); current['status'] = value; current['state'] = state; save(current)
    task = request()
    if '[late-pid]' in task['description']:  # The registry lists a session before its pid appears.
        time.sleep(0.4)
        current = load(args[1]); current['pid'] = os.getpid(); save(current)
    if '[slow-start]' in task['description']:  # The pid appears late, then idle before the prompt is picked up.
        time.sleep(0.8)
        current = load(args[1]); current.update(pid=os.getpid(), status='idle'); save(current)
        time.sleep(0.3)
        status('busy', 'working')
    # The saved MCP config has no run id; resolve the running run as acp.mcp_server does.
    run_id = env.get('ACP_RUN_ID') or next(r['id'] for r in task['runs'] if r['status'] == 'running')
    desc = task['description']
    time.sleep(0.3)  # Stay busy long enough to be observed, as a real session would.
    if '[crash-after-start]' in desc:  # Listed as working, without its pid, after the worker died.
        current = load(args[1]); current.pop('pid'); save(current)
        sys.exit(1)
    if '[hang]' in desc:
        time.sleep(90)
    if '[exit]' in desc:
        sys.exit(1)
    if '[permission]' in desc:
        status('waiting', 'blocked'); time.sleep(90)
    if '[silent]' not in desc:
        request('/comments', {'body': 'Fake background plan', 'run_id': run_id})
        if '[ask]' in desc and not any(c['in_reply_to'] for c in task['comments']):
            request('/request-input', {'question': 'Which output?', 'run_id': run_id})
        else:
            Path('output.txt').write_text('Simulated background output\n')
            request('/submit-for-review', {'summary': 'Fake background complete', 'run_id': run_id})
    status('idle', 'done')
    time.sleep(120)  # A real session stays alive, idle, until stopped.
    sys.exit(0)

assert args[0] == '--bg', args
if (Path.cwd() / '.untrusted').exists():
    print(f'Workspace not trusted. Run `claude` in {os.getcwd()} once and accept the trust prompt, then retry.',
          file=sys.stderr)
    sys.exit(1)
prompt = sys.stdin.read()
assert 'TASK SNAPSHOT' in prompt
if '[slow-launch]' in prompt:
    time.sleep(2)
resume = args[args.index('--resume') + 1] if '--resume' in args else None
flags = [a for a in args[1:] if a.startswith('--') and a != '--resume']
existing = next((e for e in entries() if e['sessionId'] == resume), None) if resume else None
copied = bool(resume) and (bool(flags) or existing is None or bool(existing.get('pid')))
if flags or existing is None:
    entry = {'name': args[args.index('--name')+1] if '--name' in args else (existing or {}).get('name', ''),
             'mcp': json.loads(args[args.index('--mcp-config')+1])['mcpServers']['acp']['env'],
             'tools': args[args.index('--allowedTools')+1]}
else:
    entry = {k: existing[k] for k in ('name', 'mcp', 'tools')}
if existing and not copied:
    entry['id'], entry['sessionId'] = existing['id'], existing['sessionId']
    print(f'note: woke session {entry["id"]} with its saved options (--name, --mcp-config, --allowedTools, --model).')
else:
    entry['sessionId'] = str(uuid.uuid4()); entry['id'] = entry['sessionId'][:8]
    if copied:
        print(f'note: started a copy of that conversation as {entry["id"]}.')
entry.update(cwd=os.getcwd(), kind='background', startedAt=int(time.time() * 1000))
save(entry)
starts = {'[crash]': 0.5, '[crash-no-record]': 0.5, '[slow-crash]': 1.5, '[stuck-start]': None}
mode = next((m for m in starts if m in prompt), None)
if mode:  # Listed as working without a pid, then settled as failed when the worker dies starting up.
    entry.update(state='working', record=mode != '[crash-no-record]')
    if starts[mode] is not None:
        entry['failAt'] = time.time() + starts[mode]
    save(entry)
    sys.stdout.buffer.write(f'backgrounded · {entry["id"]} · {entry["name"]}\n'.encode('utf-8'))
    sys.exit(0)
worker = subprocess.Popen([sys.executable, sys.argv[0], 'worker', entry['id']], cwd=os.getcwd(),
                          start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                          stderr=(state / f'{entry["id"]}.err').open('w'))
entry.update(status='busy', state='working')
if '[late-pid]' not in prompt and '[slow-start]' not in prompt:
    entry['pid'] = worker.pid
save(entry)
with (state / 'launches.jsonl').open('a') as f:
    f.write(json.dumps({'args': args, 'cwd': os.getcwd(), 'id': entry['id'], 'copied': copied,
                        'claudecode': os.environ.get('CLAUDECODE')}) + '\n')
sys.stdout.buffer.write(f'backgrounded · {entry["id"]} · {entry["name"]}\n'.encode('utf-8'))
""", encoding='utf-8')
    path.chmod(0o755)
    yield str(path)
    for file in state.glob('*.json'):
        pid = json.loads(file.read_text()).get('pid')
        if pid:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass


def background_state(fake_bg_cli) -> dict:
    state = Path(fake_bg_cli).parent / 'claude-state'
    sessions = {json.loads(f.read_text())['id']: json.loads(f.read_text()) for f in state.glob('*.json')}
    launches = [json.loads(line) for line in (state / 'launches.jsonl').read_text().splitlines()] \
        if (state / 'launches.jsonl').exists() else []
    return {'sessions': sessions, 'launches': launches}


@pytest.mark.parametrize('provider', ['claude', 'codex'])
async def test_cli_stdout_subagent_activity_is_retained_but_not_used_for_codex_pet(
        live_api, git_repo, fake_cli, provider):
    svc = live_api
    svc.settings.claude_command = svc.settings.codex_command = fake_cli
    task = svc.create_task('Observed child', description='[subagents]', provider=provider,
                           state='todo', repo_path=str(git_repo))
    from acp.activity import Activity
    pending = asyncio.create_task(Worker(svc).run_once())
    try:
        async with httpx.AsyncClient(base_url=svc.settings.base_url) as api:
            for _ in range(300):
                data = Activity(svc.store).summary()
                if data['subagents']:
                    break
                await asyncio.sleep(.01)
            assert data['running'] == 2 and data['subagents'] == 1
            await pending
            data = Activity(svc.store).summary()
            assert data['running'] == 0 and data['subagents'] == 0
            assert data['averages']['30m'] > 0
            pet = (await api.get('/api/activity')).json()
            assert pet['source'] == 'hooks' and pet['available'] is False
            assert pet['running'] == pet['waiting_on_input'] == 0
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


async def test_background_input_and_review_loop(live_api, git_repo, fake_bg_cli, monkeypatch):
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    monkeypatch.setenv('CLAUDECODE', '1')  # As when ACP is started from a Claude Code terminal.
    task = svc.create_task('Background claude', description='[ask]', provider='claude',
                           state='todo', repo_path=str(git_repo))
    worker = Worker(svc)
    assert await worker.run_once()
    assert worker.runners['claude'].background is True
    task = svc.get_task(task['id'])
    assert task['state'] == 'waiting_on_input', task['last_error']
    assert task['runs'][0]['status'] == 'succeeded'
    bg = background_state(fake_bg_cli)
    assert len(bg['launches']) == 1 and bg['launches'][0]['cwd'] == str(git_repo)
    first = bg['launches'][0]['args']
    assert first[0] == '--bg' and '--resume' not in first and first[first.index('--name') + 1] == 'Background claude'
    assert bg['launches'][0]['claudecode'] is None, 'parent Claude session markers are not inherited'
    session = bg['sessions'][bg['launches'][0]['id']]
    assert task['session_id'] == session['sessionId'] and len(session['sessionId']) == 36
    assert 'ACP_RUN_ID' not in session['mcp']
    assert session['state'] == 'stopped' and 'pid' not in session, 'the session is stopped when the run ends'
    svc.add_comment(task['id'], 'Use a text file', in_reply_to=task['blocker_comment_id'], resume=True)
    assert await worker.run_once()
    task = svc.get_task(task['id'])
    assert task['state'] == 'ready_for_review', task['last_error']
    bg = background_state(fake_bg_cli)
    assert bg['launches'][1]['args'] == ['--bg', '--resume', session['sessionId']], 'flagless resume keeps the id'
    assert bg['launches'][1]['id'] == session['id'] and not bg['launches'][1]['copied']
    assert (git_repo / 'output.txt').is_file()
    svc.request_changes(task['id'], 'Improve the output')
    await worker.run_once()
    task = svc.get_task(task['id'])
    assert task['state'] == 'ready_for_review', task['last_error']
    assert task['session_id'] == session['sessionId']
    assert len(background_state(fake_bg_cli)['launches']) == 3
    logs = sorted((svc.settings.data_dir / 'logs').glob('*.jsonl'))
    assert len(logs) == 3
    channels = {json.loads(line)['channel'] for line in logs[0].read_text().splitlines()}
    assert {'launch', 'agents', 'stop'} <= channels


async def test_background_resume_of_unknown_session_passes_options(live_api, git_repo, fake_bg_cli):
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    task = svc.create_task('Headless history', description='[late-pid]', provider='claude', state='todo',
                           repo_path=str(git_repo))
    with svc.store.transaction() as db:
        db.execute('UPDATE tasks SET session_id=? WHERE id=?', ('0f0f0f0f-aaaa-bbbb-cccc-dddddddddddd', task['id']))
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    assert task['state'] == 'ready_for_review', task['last_error']
    launch = background_state(fake_bg_cli)['launches'][0]
    assert launch['args'][:3] == ['--bg', '--resume', '0f0f0f0f-aaaa-bbbb-cccc-dddddddddddd']
    assert '--mcp-config' in launch['args'] and launch['copied']
    assert task['session_id'] == background_state(fake_bg_cli)['sessions'][launch['id']]['sessionId']


async def test_background_resume_stops_a_live_session_first(live_api, git_repo, fake_bg_cli):
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    task = svc.create_task('Left running', description='[hang]', provider='claude', repo_path=str(git_repo))
    mcp = json.dumps({'mcpServers': {'acp': {'env': {'ACP_URL': svc.settings.base_url, 'ACP_TASK_ID': task['id']}}}})
    launched = subprocess.run([fake_bg_cli, '--bg', '--name', 'Left running', '--mcp-config', mcp,
                               '--allowedTools', 'Read'], input=b'TASK SNAPSHOT', cwd=git_repo,
                              capture_output=True, check=True)
    short = launched.stdout.decode().split()[2]
    stale = background_state(fake_bg_cli)['sessions'][short]
    assert stale.get('pid'), 'the leftover session is still running, as after a server crash'
    with svc.store.transaction() as db:
        db.execute('UPDATE tasks SET session_id=?, description=? WHERE id=?', (stale['sessionId'], '', task['id']))
    svc.move_task(task['id'], 'todo')
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    assert task['state'] == 'ready_for_review', task['last_error']
    bg = background_state(fake_bg_cli)
    assert bg['launches'][1]['args'] == ['--bg', '--resume', stale['sessionId']] and not bg['launches'][1]['copied']
    assert task['session_id'] == stale['sessionId']
    for _ in range(100):
        if subprocess.run(['ps', '-p', str(stale['pid']), '-o', 'stat='], capture_output=True,
                          text=True).stdout.strip() in ('', 'Z'):
            break
        await asyncio.sleep(.03)
    else:
        pytest.fail('the stale worker survived the pre-resume claude stop')


async def test_background_session_exit_without_report_fails(live_api, git_repo, fake_bg_cli):
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    task = svc.create_task('Exit', description='[exit]', provider='claude', state='todo', repo_path=str(git_repo))
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    assert task['state'] == 'waiting_on_input'
    assert 'exited without reporting' in task['last_error'] and '--resume' in task['last_error']
    assert task['runs'][0]['status'] == 'failed'


@pytest.mark.parametrize('marker, reason', [('[crash]', ': exit 1 before init'), ('[crash-no-record]', '')])
async def test_background_session_failure_reports_claudes_reason(live_api, git_repo, fake_bg_cli, marker, reason):
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    task = svc.create_task('Crash', description=marker, provider='claude', state='todo', repo_path=str(git_repo))
    started = time.monotonic()
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    session = next(iter(background_state(fake_bg_cli)['sessions'].values()))
    assert task['state'] == 'waiting_on_input'
    assert task['runs'][0]['status'] == 'failed'
    assert task['last_error'] == f"Claude background session {session['id']} failed{reason}. See the run log."
    assert time.monotonic() - started < 10  # A settled failure does not wait out the startup grace period.


async def test_background_slow_startup_failure_still_reports_claudes_reason(live_api, git_repo, fake_bg_cli,
                                                                            monkeypatch):
    monkeypatch.setattr(runners, 'STARTUP_GRACE_SECONDS', 0.3)  # Claude settles the session after the grace.
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    task = svc.create_task('Slow crash', description='[slow-crash]', provider='claude', state='todo',
                           repo_path=str(git_repo))
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    session = next(iter(background_state(fake_bg_cli)['sessions'].values()))
    assert task['last_error'] == f"Claude background session {session['id']} failed: exit 1 before init. " \
                                 f"See the run log."


async def test_background_stuck_startup_gives_up_at_the_startup_limit(live_api, git_repo, fake_bg_cli, monkeypatch):
    monkeypatch.setattr(runners, 'STARTUP_LIMIT_SECONDS', 1)  # The 30-second grace is not added on top.
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    svc.settings.run_timeout_seconds = 5  # Waiting past the startup limit would end here instead.
    task = svc.create_task('Stuck', description='[stuck-start]', provider='claude', state='todo',
                           repo_path=str(git_repo))
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    assert task['runs'][0]['status'] == 'failed'
    assert 'exited without reporting' in task['last_error']


async def test_background_slow_start_keeps_the_grace_after_the_worker_starts(live_api, git_repo, fake_bg_cli,
                                                                            monkeypatch):
    # The worker's pid appears after the grace from launch, and it idles for 0.3 seconds before going
    # busy: a grace that starts when the pid is first seen must outlast that idle plus a poll's jitter.
    monkeypatch.setattr(runners, 'STARTUP_GRACE_SECONDS', 0.5)
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    task = svc.create_task('Slow start', description='[slow-start]', provider='claude', state='todo',
                           repo_path=str(git_repo))
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    assert task['state'] == 'ready_for_review', task['last_error']


async def test_background_crash_after_start_does_not_wait_for_the_startup_limit(live_api, git_repo, fake_bg_cli):
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    svc.settings.run_timeout_seconds = 5  # Treating the dead session as still starting would end here instead.
    task = svc.create_task('Crash after start', description='[crash-after-start]', provider='claude',
                           state='todo', repo_path=str(git_repo))
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    assert 'exited without reporting' in task['last_error']


async def test_background_turn_without_report_fails_and_stops_session(live_api, git_repo, fake_bg_cli):
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    task = svc.create_task('Silent', description='[silent]', provider='claude', state='todo',
                           repo_path=str(git_repo))
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    assert task['state'] == 'waiting_on_input'
    assert task['runs'][0]['status'] == 'failed'
    session = next(iter(background_state(fake_bg_cli)['sessions'].values()))
    assert f"claude attach {session['id']}" in task['last_error']
    assert session['state'] == 'stopped' and 'pid' not in session


async def test_background_cancel_stops_session(live_api, git_repo, fake_bg_cli):
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    task = svc.create_task('Cancel', description='[hang]', provider='claude', state='todo', repo_path=str(git_repo))
    running = asyncio.create_task(Worker(svc).run_once())
    for _ in range(300):
        if svc.get_task(task['id'])['session_id']:
            break
        await asyncio.sleep(.03)
    svc.cancel(task['id'])
    await asyncio.wait_for(running, 5)
    task = svc.get_task(task['id'])
    assert task['state'] == 'planning'
    assert task['runs'][0]['status'] == 'cancelled'
    session = next(iter(background_state(fake_bg_cli)['sessions'].values()))
    assert session['state'] == 'stopped' and 'pid' not in session


async def test_background_server_stop_stops_session(live_api, git_repo, fake_bg_cli):
    """`acp stop` cancels the worker task; the session must not outlive it."""
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    task = svc.create_task('Server stop', description='[hang]', provider='claude', state='todo',
                           repo_path=str(git_repo))
    running = asyncio.create_task(Worker(svc).run_once())
    for _ in range(300):
        if svc.get_task(task['id'])['session_id']:
            break
        await asyncio.sleep(.03)
    running.cancel()
    with pytest.raises(asyncio.CancelledError):
        await running
    task = svc.get_task(task['id'])
    assert task['runs'][0]['status'] == 'interrupted'
    session = next(iter(background_state(fake_bg_cli)['sessions'].values()))
    assert session['state'] == 'stopped' and 'pid' not in session


async def test_background_stop_failure_fails_the_run_loudly(live_api, git_repo, fake_bg_cli):
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    (Path(fake_bg_cli).parent / 'claude-state' / 'stop-fails').write_text('')
    task = svc.create_task('Unstoppable', description='[ask]', provider='claude', state='todo',
                           repo_path=str(git_repo))
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    assert task['runs'][0]['status'] == 'failed'
    session = next(iter(background_state(fake_bg_cli)['sessions'].values()))
    assert session.get('pid'), 'the fake refused to stop, as a broken CLI would'
    assert task['last_error'] == (f"Claude background session {session['id']} is still running after "
                                  f"`claude stop`. Stop it with `claude stop {session['id']}` before retrying.")


async def test_background_run_limit_covers_the_launch(live_api, git_repo, fake_bg_cli):
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    svc.settings.run_timeout_seconds = .5
    task = svc.create_task('Slow launch', description='[slow-launch]', provider='claude', state='todo',
                           repo_path=str(git_repo))
    before = time.monotonic()
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    assert task['runs'][0]['status'] == 'failed' and 'claude --bg did not finish within' in task['last_error']
    assert time.monotonic() - before < 3, 'the launch itself is bounded by the run limit (1 s floor)'


async def test_claude_call_timeout_names_the_command(tmp_path, settings):
    slow = tmp_path / 'slow-claude'
    slow.write_text(f'#!{sys.executable}\nimport time; time.sleep(30)\n')
    slow.chmod(0o755)
    settings.claude_command = str(slow)
    with pytest.raises(TimeoutError, match='claude agents did not finish within 0.2 seconds'):
        await claude(settings, 'agents', '--json', timeout=0.2)


async def test_background_timeout_stops_session(live_api, git_repo, fake_bg_cli):
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    svc.settings.run_timeout_seconds = .4
    task = svc.create_task('Timeout', description='[hang]', provider='claude', state='todo', repo_path=str(git_repo))
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    assert 'run limit' in task['last_error']
    assert task['runs'][0]['status'] == 'failed'
    session = next(iter(background_state(fake_bg_cli)['sessions'].values()))
    assert session['state'] == 'stopped' and 'pid' not in session


async def test_background_permission_prompt_keeps_run_alive(live_api, git_repo, fake_bg_cli):
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    svc.settings.run_timeout_seconds = 1.5
    task = svc.create_task('Permission', description='[permission]', provider='claude', state='todo',
                           repo_path=str(git_repo))
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    assert 'run limit' in task['last_error'], 'a waiting session is left for the user until the run limit'
    assert any('claude attach' in c['body'] and 'permission prompt' in c['body'] for c in task['comments'])


async def test_background_untrusted_workspace_fails_clearly(live_api, git_repo, fake_bg_cli):
    svc = live_api
    svc.settings.claude_command = fake_bg_cli
    (git_repo / '.untrusted').write_text('')
    task = svc.create_task('Untrusted', provider='claude', state='todo', repo_path=str(git_repo))
    await Worker(svc).run_once()
    task = svc.get_task(task['id'])
    assert task['state'] == 'waiting_on_input'
    assert task['last_error'].startswith('Workspace not trusted.')
    assert background_state(fake_bg_cli)['launches'] == []


async def test_claude_mode_probe_and_override(settings, fake_cli, fake_bg_cli):
    settings.claude_command = fake_cli  # Its --help has no --bg; the headless claude tests run through this.
    runner = CLIRunner()
    assert await runner.claude_mode(settings) == 'headless' and runner.background is False
    settings.claude_command = fake_bg_cli
    runner = CLIRunner()
    assert await runner.claude_mode(settings) == 'background'
    settings.claude_command = '/no/such/claude'
    assert await runner.claude_mode(settings) == 'background', 'probed once per service start'
    settings.claude_run_mode = 'headless'
    runner = CLIRunner()
    assert await runner.claude_mode(settings) == 'headless' and runner.background is None, 'no probe'
    with pytest.raises(ValueError, match='CLAUDE_RUN_MODE'):
        Settings(claude_run_mode='sometimes').validate()


def test_background_args_keep_saved_options(settings):
    fresh = {'id': 'task', 'title': 'T' * 80, 'session_id': None}
    args = background_args(settings, fresh, saved_session=False)
    assert args[:3] == ['--bg', '--name', 'T' * 60]
    env = json.loads(args[args.index('--mcp-config') + 1])['mcpServers']['acp']['env']
    assert env == {'ACP_URL': settings.base_url, 'ACP_TASK_ID': 'task'}
    assert args[args.index('--allowedTools') + 1].endswith('mcp__acp__submit_for_review')
    resumed = dict(fresh, session_id='abcd1234-0000-0000-0000-000000000000')
    assert background_args(settings, resumed, saved_session=True) == \
        ['--bg', '--resume', 'abcd1234-0000-0000-0000-000000000000']
    assert background_args(settings, resumed, saved_session=False)[:3] == \
        ['--bg', '--resume', 'abcd1234-0000-0000-0000-000000000000']
    assert '--mcp-config' in background_args(settings, resumed, saved_session=False)
    settings.claude_model = 'opus'
    assert background_args(settings, fresh, saved_session=False)[-2:] == ['--model', 'opus']


def test_mcp_server_resolves_the_running_run(live_api, monkeypatch):
    from acp import mcp_server
    svc = live_api
    task = svc.create_task('Lookup', provider='claude', state='todo')
    monkeypatch.setenv('ACP_URL', svc.settings.base_url)
    monkeypatch.setenv('ACP_TASK_ID', task['id'])
    monkeypatch.delenv('ACP_RUN_ID', raising=False)
    with pytest.raises(RuntimeError, match='no longer active'):
        mcp_server.add_comment('too early')
    run = svc.claim_next()
    svc.mark_started(run['id'])
    comment = mcp_server.add_comment('from a resumed background session')
    assert comment['body'] == 'from a resumed background session'
    assert mcp_server.get_task()['state'] == 'in_progress'
    svc.remember_session(run['id'], 'aaaaaaaa-1111-2222-3333-444444444444')
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'aaaaaaaa-1111-2222-3333-444444444444')  # Set by Claude.
    assert mcp_server.add_comment('from the task\'s own session')['body'].endswith('own session')
    monkeypatch.setenv('CLAUDE_CODE_SESSION_ID', 'bbbbbbbb-1111-2222-3333-444444444444')
    with pytest.raises(RuntimeError, match='no longer the agent'):
        mcp_server.add_comment('from a stale session that was reopened by hand')
    assert mcp_server.get_task()['state'] == 'in_progress', 'reads are still allowed'
    monkeypatch.setenv('ACP_RUN_ID', 'a-stale-run')  # An explicit run id is still checked, as before.
    with pytest.raises(Exception, match='no longer active'):
        mcp_server.add_comment('stale')


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
        # Both processes are in the killed group. Linux may briefly retain the child as a
        # zombie, and init can reap it at any moment: before the open (ENOENT) or between
        # the open and the read (ESRCH). Either means the child is already gone.
        try:
            status = Path(f'/proc/{first["pid"]}/status').read_text()
        except (FileNotFoundError, ProcessLookupError):
            status = None
        if status is not None:
            assert 'Z (zombie)' in status
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
