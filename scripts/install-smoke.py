"""Install real release assets over HTTP, then exercise the installed CLI.

Default uses the simulated provider. --live uses authenticated Codex and makes
three real agent runs in a disposable repository. --bootstrap tests without uv
on PATH. Artifacts and logs are retained under .acp/install-smoke-*.
"""
import argparse
import functools
import http.server
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument('--live', action='store_true')
parser.add_argument('--bootstrap', action='store_true')
args = parser.parse_args()
base = ROOT / '.acp' / ('install-smoke-' + time.strftime('%Y%m%d-%H%M%S'))
base.mkdir(parents=True)
(base / 'outside-repo').mkdir()
env = {k: v for k, v in os.environ.items() if not k.startswith('ACP_')
       and k not in {'PYTHONPATH', 'PYTHONHOME', 'DEFAULT_PROVIDER', 'DEFAULT_REPO'}}
env.update(ACP_HOME=str(base / 'home'), UV_TOOL_DIR=str(base / 'tools'),
           UV_TOOL_BIN_DIR=str(base / 'bin'), POLL_SECONDS='0.05', RUN_TIMEOUT_SECONDS='240')
handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(ROOT / 'dist/release'))
httpd = http.server.ThreadingHTTPServer(('127.0.0.1', 0), handler)
thread = threading.Thread(target=httpd.serve_forever, daemon=True)
thread.start()
env['ACP_RELEASE_BASE_URL'] = f'http://127.0.0.1:{httpd.server_port}'
exe = str(base / 'bin/acp')
report = {'evidence_dir': str(base), 'provider': 'codex' if args.live else 'demo',
          'bootstrap_uv': args.bootstrap, 'stages': []}


def install(expect_success=True):
    install_env = env.copy()
    if args.bootstrap:
        install_env['PATH'] = f'{fake_bin}:/usr/bin:/bin:/usr/sbin:/sbin'
    # The script is delivered through stdin, matching the documented curl | sh command.
    downloaded = subprocess.run(['curl', '-fLsS', env['ACP_RELEASE_BASE_URL'] + '/install.sh'],
                                check=True, capture_output=True)
    result = subprocess.run(['sh'], input=downloaded.stdout, env=install_env,
                            cwd=base / 'outside-repo', capture_output=True, timeout=180)
    with (base / 'installer.log').open('ab') as log:
        log.write(result.stdout + result.stderr)
    assert (result.returncode == 0) == expect_success, (result.stdout + result.stderr).decode()
    if not expect_success:
        assert b'acp stop' in result.stderr
    return (result.stdout + result.stderr).decode()


def hook_state():
    """Every hook file and manifest that exists; reinstalls and updates must leave them byte-identical."""
    return {str(path): path.read_bytes() for path in hook_files if path.exists()}


def cli(*argv, timeout=35):
    result = subprocess.run([exe, *argv], env=env, cwd=base / 'outside-repo',
                            capture_output=True, text=True, timeout=timeout)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout)


def wait_for(task_id, state, runs):
    deadline = time.monotonic() + 300
    last = None
    while time.monotonic() < deadline:
        task = cli('show', task_id)
        statuses = [run['status'] for run in task['runs']]
        progress = (task['state'], statuses)
        if progress != last:
            print(json.dumps({'state': task['state'], 'runs': statuses}), flush=True)
            last = progress
        assert not any(s in {'failed', 'interrupted', 'cancelled'} for s in statuses), task['last_error']
        if task['state'] == state and len(statuses) == runs and all(s == 'succeeded' for s in statuses):
            report['stages'].append({'state': state, 'runs': runs, 'session': task['session_id'],
                                     'workspace': task['workspace'], 'branch': task['branch']})
            return task
        time.sleep(.4)
    raise AssertionError(f'Timeout waiting for {state}')


def verify_output(task, trim=False):
    output = next(a['url'] for a in reversed(task['artifacts']) if a['kind'] == 'file')
    workspace = Path(output).parent
    run = subprocess.run([sys.executable, '-m', 'unittest', '-v'], cwd=workspace,
                         capture_output=True, text=True, timeout=30)
    assert run.returncode == 0, run.stdout + run.stderr
    (base / 'unittest.log').write_text(run.stdout + run.stderr)
    code = 'from greeting import greet; assert greet("Ada") == "Hello, Ada!"'
    if trim:
        code += '; assert greet("  Ada  ") == "Hello, Ada!"'
    subprocess.run([sys.executable, '-c', code], cwd=workspace, check=True, timeout=15)


# Separate Claude and Codex homes: never alter the user's hooks or hook trust. Fake agent CLIs
# on PATH let setup detect both and hook them automatically. A live run needs the real Codex and
# its own home, so it opts out of automatic hooks and installs the Codex ones explicitly instead.
fake_bin, codex_home, claude_home = base / 'fake-bin', base / 'codex-home', base / 'claude-home'
fake_bin.mkdir()
codex_home.mkdir()
hooks_file, settings_file = codex_home / 'hooks.json', claude_home / 'settings.json'
manifest = base / 'home/data/codex-activity.hooks.json'
hook_files = [hooks_file, manifest, settings_file, base / 'home/data/claude-activity.hooks.json']
hooks_file.write_text(json.dumps({'hooks': {'Stop': [{'hooks': [
    {'type': 'command', 'command': 'echo unrelated-hook'}]}]}}))
if args.live:
    env['ACP_ACTIVITY_HOOKS'] = '0'
else:
    for name in ('claude', 'codex'):
        (fake_bin / name).write_text('#!/bin/sh\nexit 0\n')
        (fake_bin / name).chmod(0o755)
    env.update(PATH=f'{fake_bin}:{env["PATH"]}', CODEX_HOME=str(codex_home), CLAUDE_CONFIG_DIR=str(claude_home))

try:
    output = install()
    config = base / 'home/config.env'
    original = config.read_bytes()
    if args.live:
        assert 'skipped: ACP_ACTIVITY_HOOKS=0' in output and 'trust the ACP handlers' not in output, output
        cli('activity', 'install', '--codex-home', str(codex_home))
    else:
        text = config.read_text()
        assert 'DEFAULT_PROVIDER=claude\n' in text, text
        assert f'CLAUDE_COMMAND={fake_bin}/claude\n' in text and f'CODEX_COMMAND={fake_bin}/codex\n' in text, text
        assert '"found": true' in output and '"status": "installed"' in output, output
        assert 'open /hooks in the Codex CLI, review and trust the ACP handlers' in output, output
        assert 'acp.claude_activity' in settings_file.read_text()
    hooks = json.loads(hooks_file.read_text())['hooks']
    assert hooks['Stop'][0]['hooks'][0]['command'] == 'echo unrelated-hook'
    assert 'acp.codex_activity' in hooks['Stop'][1]['hooks'][0]['command']
    hooks_before = hook_state()
    assert set(hooks_before) == {str(path) for path in hook_files[:2 if args.live else 4]}
    assert 'trust the ACP handlers' not in install()  # nothing changed, so no reminder
    assert hook_state() == hooks_before
    assert config.read_bytes() == original
    info = cli('start', '--port', '0')
    assert info['healthy']
    assert cli('start')['pid'] == info['pid']
    opener = base / 'browser-opener'
    opener.write_text('#!/bin/sh\nprintf "%s" "$1" > "$BROWSER_CAPTURE"\n')
    opener.chmod(0o755)
    env['BROWSER'] = str(opener) + ' %s'
    env['BROWSER_CAPTURE'] = str(base / 'opened-url')
    cli('open')
    assert (base / 'opened-url').read_text() == info['url']
    install(expect_success=False)
    with httpx.Client(base_url=info['url'], trust_env=False) as client:
        assert '<title>ACP ' in client.get('/').text
        assert client.get('/static/app.js').status_code == 200
    description = '[ask] Exercise the installed demo input and review flow.'
    extra = []
    if args.live:
        source = base / 'source'
        source.mkdir()
        (source / 'README.md').write_text('Disposable ACP installation verification repository.\n')
        (source / '.gitignore').write_text('__pycache__/\n')
        for argv in [('init', '-b', 'main'), ('config', 'user.name', 'ACP Smoke'),
                     ('config', 'user.email', 'acp-smoke@example.invalid'), ('add', '.'),
                     ('commit', '-m', 'Initialize smoke repository')]:
            subprocess.run(['git', *argv], cwd=source, check=True, capture_output=True)
        extra = ['--repo', str(source)]
        description = '''This is an authorized end-to-end verification task in a disposable repository.
Choose your workspace within this disposable repository using your normal workflow.
Use task MCP tools, shell/file tools, and Python standard library.
Do not delegate, push, deploy, create PRs, install dependencies, or access other projects.
The design below is already approved. First call get_task and add_comment with a brief plan.
On the FIRST run, before editing, use request_input to ask exactly:
"Should the greeting end with an exclamation mark or a period?" Then stop.
After the answer, create greeting.py with greet(name: str) -> str returning Hello, <name><punctuation>.
Create test_greeting.py using unittest, including an exact expected greeting assertion.
Run python3 -m unittest -v. Submit using submit_for_review with kind=file and the absolute greeting.py path.
If changes are requested, update the SAME files/tests, rerun unittest, and resubmit. Preserve the session and branch.'''
    task = cli('create', 'Installed ACP task verification', '--provider', report['provider'],
               '--description', description, '--state', 'todo', *extra)
    task_id = task['id']
    report['task_id'] = task_id
    task = wait_for(task_id, 'waiting_on_input', 1)
    cli('answer', task_id, 'Use an exclamation mark: greet("Ada") must return "Hello, Ada!".',
        '--blocker-id', task['blocker_comment_id'])
    task = wait_for(task_id, 'ready_for_review', 2)
    if args.live:
        verify_output(task)
    cli('changes', task_id, 'Strip leading/trailing whitespace from the name. Add an exact test that '
        'greet("  Ada  ") returns "Hello, Ada!". Run unittest and resubmit.')
    task = wait_for(task_id, 'ready_for_review', 3)
    if args.live:
        verify_output(task, trim=True)
        assert len({stage['session'] for stage in report['stages']}) == 1
        assert report['stages'][0]['session']
        assert not (base / 'home/data/worktrees').exists()
    cli('move', task_id, 'done')
    with httpx.stream('GET', info['url'] + '/api/events/stream', timeout=20, trust_env=False) as stream:
        assert stream.status_code == 200
        cli('stop')
    assert not cli('status')['running']
    install()
    assert config.read_bytes() == original
    cli('start', '--port', '0')
    assert hook_state() == hooks_before
    command = json.loads(hooks_before[str(manifest)])['command']
    callback = subprocess.run(command, shell=True, input=json.dumps({
        'hook_event_name': 'SessionStart', 'session_id': 'installer-verification'}),
        text=True, capture_output=True, cwd=base / 'outside-repo', env=env, timeout=10)
    assert callback.returncode == 0 and json.loads(callback.stdout) == {}, callback.stderr
    assert cli('activity', 'status')['observed_since'] is not None
    # `acp update` against this same release: current, then a reinstall that restarts the service.
    check = cli('update', '--check')
    assert check['update_available'] is False and check['installed_version'] == check['target_version'], check
    running = cli('status')
    updated = cli('update', '--reinstall', timeout=240)
    assert updated['updated'] and updated['installed_version'] == check['target_version'], updated
    assert updated['restarted'] == {'service': True, 'overlay': False}, updated
    expected = 'skipped: ACP_ACTIVITY_HOOKS=0' if args.live else 'already installed'
    assert {name: entry['status'] for name, entry in updated['hooks'].items()} == \
        {'codex': expected, 'claude': expected}, updated
    assert updated['warnings'] == [], updated
    assert hook_state() == hooks_before
    after = cli('status')
    assert after['healthy'] and after['url'] == running['url'] and after['pid'] != running['pid'], after
    assert config.read_bytes() == original
    report['update_verified'] = True
    report['hooks'] = updated['hooks']
    report['reinstall_preserved_hooks'] = True
    report['installed_hook_callback_verified'] = True
    saved = cli('show', task_id)
    assert saved['state'] == 'done'
    (base / 'task.json').write_text(json.dumps(saved, indent=2))
    report.update(result='passed', final_state='done', reinstall_preserved_data=True)
except BaseException as exc:
    report.update(result='failed', error=str(exc))
    raise
finally:
    if Path(exe).exists():
        subprocess.run([exe, 'stop'], env=env, cwd=base, capture_output=True, timeout=30)
    httpd.shutdown()
    httpd.server_close()
    (base / 'report.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2), flush=True)
