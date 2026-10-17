"""Exercise the installed-style CLI across real process and HTTP boundaries."""
import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def cli(tmp_path):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith('ACP_') and k not in {'DEFAULT_PROVIDER', 'DEFAULT_REPO'}}
    env.update(ACP_HOME=str(tmp_path / 'home'), PYTHONPATH=str(ROOT), POLL_SECONDS='0.02')

    def run(*args, check=True):
        result = subprocess.run([sys.executable, '-m', 'acp.cli', *args], env=env,
                                cwd=tmp_path, capture_output=True, text=True, timeout=35)
        if check:
            assert result.returncode == 0, result.stdout + result.stderr
        return result
    run.env = env
    run.home = tmp_path / 'home'
    yield run
    run('stop', check=False)


def wait_task(cli, task_id, state):
    for _ in range(100):
        task = json.loads(cli('show', task_id).stdout)
        if task['state'] == state and all(r['status'] != 'running' for r in task['runs']):
            return task
        time.sleep(.05)
    pytest.fail(f'Task did not reach {state}: {task}')


def test_setup_preserves_config_and_service_runs_full_demo_flow(cli):
    cli('setup')
    config = cli.home / 'config.env'
    config.write_text(config.read_text() + '\n# preserve my settings\n')
    before = config.read_bytes()
    cli('setup')
    assert config.read_bytes() == before
    assert config.stat().st_mode & 0o077 == 0
    first = json.loads(cli('start', '--port', '0').stdout)
    second = json.loads(cli('start').stdout)
    assert first['pid'] == second['pid']
    assert first['running'] and first['healthy']
    task = json.loads(cli('create', 'Installed demo', '--provider', 'demo',
                          '--description', '[ask]', '--state', 'todo').stdout)
    task_id = task['id']
    wait_task(cli, task_id, 'waiting_on_input')
    cli('answer', task_id, 'Use a greeting')
    wait_task(cli, task_id, 'ready_for_review')
    cli('changes', task_id, 'Improve the greeting')
    reviewed = wait_task(cli, task_id, 'ready_for_review')
    assert len(reviewed['runs']) == 3
    cli('move', task_id, 'done')
    assert cli('logs').stdout
    # A live EventSource connection must not hang graceful shutdown.
    with httpx.stream('GET', first['url'] + '/api/events/stream', timeout=15) as stream:
        assert stream.status_code == 200
        cli('stop')
    assert not json.loads(cli('status').stdout)['running']
    cli('stop')
    cli('start', '--port', '0')
    assert json.loads(cli('show', task_id).stdout)['state'] == 'done'


def test_control_rejects_other_clients_and_occupied_port(cli):
    cli('setup')
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        sock.listen()
        result = cli('start', '--port', str(sock.getsockname()[1]), check=False)
        assert result.returncode != 0
        assert not json.loads(cli('status').stdout)['running']
    status = json.loads(cli('start', '--port', '0').stdout)
    assert httpx.post(status['url'] + '/_acp/stop', timeout=5).status_code == 403
    assert json.loads(cli('status').stdout)['healthy']


def test_stale_metadata_does_not_signal_unrelated_process(cli):
    cli('setup')
    data = cli.home / 'data'
    (data / 'service.json').write_text(json.dumps({
        'pid': os.getpid(), 'url': 'http://127.0.0.1:1', 'token': 'old'}))
    assert not json.loads(cli('status').stdout)['running']
    cli('stop')
    assert json.loads(cli('start', '--port', '0').stdout)['healthy']


def test_concurrent_starts_create_one_worker(cli):
    from concurrent.futures import ThreadPoolExecutor
    cli('setup')
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: json.loads(cli('start', '--port', '0').stdout), range(2)))
    assert results[0]['pid'] == results[1]['pid']
    assert all(result['healthy'] for result in results)


def test_stop_interrupts_active_agent_and_releases_worker(cli, tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    for args in [('init', '-b', 'main'), ('config', 'user.name', 'Test'),
                 ('config', 'user.email', 'test@example.invalid')]:
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    (repo / 'README.md').write_text('Test repository\n')
    subprocess.run(['git', 'add', '.'], cwd=repo, check=True)
    subprocess.run(['git', 'commit', '-m', 'initial'], cwd=repo, check=True, capture_output=True)
    marker = tmp_path / 'agent.pid'
    fake = tmp_path / 'agent'
    fake.write_text(f'#!{sys.executable}\nimport os, time\nfrom pathlib import Path\n'
                    f'Path({str(marker)!r}).write_text(str(os.getpid()))\ntime.sleep(120)\n')
    fake.chmod(0o755)
    cli.env['CODEX_COMMAND'] = str(fake)
    cli('setup')
    cli('start', '--port', '0')
    task = json.loads(cli('create', 'Interrupt me', '--provider', 'codex',
                          '--repo', str(repo), '--state', 'todo').stdout)
    for _ in range(100):
        if marker.exists():
            break
        time.sleep(.05)
    assert marker.exists()
    pid = int(marker.read_text())
    cli('stop')
    processes = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True)
    assert not processes.stdout.strip() or processes.stdout.strip().startswith('Z')
    cli('start', '--port', '0')
    task = json.loads(cli('show', task['id']).stdout)
    assert task['runs'][0]['status'] == 'interrupted'


def test_cli_keeps_managed_instance_url_when_worker_reports_error(monkeypatch, tmp_path, capsys):
    from acp import cli as module, daemon
    monkeypatch.setenv('ACP_HOME', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['acp', 'list'])
    monkeypatch.setattr(daemon, 'status', lambda _: {
        'running': True, 'reachable': True, 'healthy': False, 'url': 'http://127.0.0.1:49876'})
    targets = []
    monkeypatch.setattr(module, 'request', lambda base, *args: targets.append(base) or [])
    module.main()
    assert targets == ['http://127.0.0.1:49876']


def test_cli_refuses_to_fall_back_when_managed_service_unreachable(monkeypatch, tmp_path):
    from acp import cli as module, daemon
    monkeypatch.setenv('ACP_HOME', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['acp', 'create', 'Do not send elsewhere'])
    monkeypatch.setattr(daemon, 'status', lambda _: {
        'running': True, 'reachable': False, 'healthy': False, 'url': 'http://127.0.0.1:49876'})
    targets = []
    monkeypatch.setattr(module, 'request', lambda base, *args: targets.append(base) or [])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 1
    assert not targets


def test_cli_refuses_requests_after_selected_service_stops(monkeypatch, tmp_path):
    from acp import cli as module, daemon
    monkeypatch.setenv('ACP_HOME', str(tmp_path))
    monkeypatch.setattr(sys, 'argv', ['acp', 'list'])
    monkeypatch.setattr(daemon, 'status', lambda _: {
        'running': False, 'reachable': False, 'healthy': False})
    targets = []
    monkeypatch.setattr(module, 'request', lambda base, *args: targets.append(base) or [])
    with pytest.raises(SystemExit) as exc:
        module.main()
    assert exc.value.code == 1
    assert not targets


def test_foreground_serve_uses_same_identity_and_can_be_stopped(cli, tmp_path):
    cli('setup')
    process = subprocess.Popen([sys.executable, '-m', 'acp.cli', 'serve', '--port', '0'],
                               env=cli.env, cwd=tmp_path, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        for _ in range(50):
            status = json.loads(cli('status').stdout)
            if status.get('reachable'):
                break
            assert process.poll() is None
            time.sleep(.05)
        assert status.get('reachable')
        assert json.loads(cli('list').stdout) == []
        cli('stop')
        assert process.wait(timeout=10) == 0
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
