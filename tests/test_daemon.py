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
    env = {k: v for k, v in os.environ.items() if not k.startswith('ACP_') and k not in
           {'DEFAULT_PROVIDER', 'DEFAULT_REPO', 'CLAUDE_COMMAND', 'CODEX_COMMAND'}}
    # Setup installs agent hooks: keep every home under tmp_path, never the developer's own.
    env.update(ACP_HOME=str(tmp_path / 'home'), PYTHONPATH=str(ROOT), POLL_SECONDS='0.02',
               HOME=str(tmp_path / 'user'), CLAUDE_CONFIG_DIR=str(tmp_path / 'user/.claude'),
               CODEX_HOME=str(tmp_path / 'user/.codex'))

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


def fake_agents(tmp_path, *names):
    """A PATH holding only the named fake CLIs (plus system tools), so detection is deterministic."""
    bin_dir = tmp_path / 'fake-bin'
    bin_dir.mkdir(exist_ok=True)
    for stale in bin_dir.iterdir():
        stale.unlink()
    for name in names:
        (bin_dir / name).write_text('#!/bin/sh\nexit 0\n')
        (bin_dir / name).chmod(0o755)
    return f'{bin_dir}:/usr/bin:/bin'


def test_setup_detects_agents_and_installs_their_hooks_once(cli, tmp_path):
    cli.env['PATH'] = fake_agents(tmp_path, 'claude', 'codex')
    result = json.loads(cli('setup').stdout)
    claude, codex = str(tmp_path / 'fake-bin/claude'), str(tmp_path / 'fake-bin/codex')
    config = (cli.home / 'config.env').read_text()
    assert 'DEFAULT_PROVIDER=claude\n' in config
    assert f'CLAUDE_COMMAND={claude}\n' in config and f'CODEX_COMMAND={codex}\n' in config
    assert result['created'] is True
    assert result['agents'] == {'claude': {'command': claude, 'found': True},
                                'codex': {'command': codex, 'found': True}}
    settings_file, hooks_file = tmp_path / 'user/.claude/settings.json', tmp_path / 'user/.codex/hooks.json'
    assert result['hooks']['claude']['status'] == 'installed' and result['hooks']['codex']['status'] == 'installed'
    assert result['hooks']['claude']['file'] == str(settings_file) and result['hooks']['codex']['file'] == str(hooks_file)
    assert result['hooks']['codex']['trust_required'] is True and '/hooks' in result['hooks']['codex']['next_step']
    assert 'trust_required' not in result['hooks']['claude'] and 'new Claude Code session' in result['hooks']['claude']['next_step']
    # The same hook entries `acp activity install` writes, bound to the interpreter that ran setup.
    for path, module in [(settings_file, 'acp.claude_activity'), (hooks_file, 'acp.codex_activity')]:
        command = json.loads(path.read_text())['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']
        assert command.startswith(sys.executable) and module in command
    manifests = [cli.home / 'data' / f'{name}-activity.hooks.json' for name in ('claude', 'codex')]
    before = [path.read_bytes() for path in (settings_file, hooks_file, *manifests)]
    again = json.loads(cli('setup').stdout)
    assert again['created'] is False and (cli.home / 'config.env').read_text() == config
    assert again['hooks'] == {'codex': {'status': 'already installed', 'home': str(hooks_file.parent), 'file': str(hooks_file)},
                              'claude': {'status': 'already installed', 'home': str(settings_file.parent), 'file': str(settings_file)}}
    assert [path.read_bytes() for path in (settings_file, hooks_file, *manifests)] == before
    assert not list((tmp_path / 'user/.claude').glob('settings.acp-backup-*'))  # nothing to back up: ACP wrote the file


def test_setup_without_agents_skips_hooks_and_later_agents_get_them(cli, tmp_path):
    cli.env['PATH'] = fake_agents(tmp_path)
    result = json.loads(cli('setup').stdout)
    config = cli.home / 'config.env'
    assert 'DEFAULT_PROVIDER=claude\n' in config.read_text()
    assert 'CLAUDE_COMMAND=claude\n' in config.read_text() and 'CODEX_COMMAND=codex\n' in config.read_text()
    assert result['agents'] == {'claude': {'command': 'claude', 'found': False},
                                'codex': {'command': 'codex', 'found': False}}
    assert result['hooks'] == {'codex': {'status': 'skipped: not found'}, 'claude': {'status': 'skipped: not found'}}
    assert not (tmp_path / 'user').exists() and not list((cli.home / 'data').glob('*.hooks.json'))
    # Codex alone becomes the default provider.
    config.unlink()
    cli.env['PATH'] = fake_agents(tmp_path, 'codex')
    result = json.loads(cli('setup').stdout)
    assert 'DEFAULT_PROVIDER=codex\n' in config.read_text() and 'CLAUDE_COMMAND=claude\n' in config.read_text()
    assert result['hooks']['codex']['status'] == 'installed' and result['hooks']['claude'] == {'status': 'skipped: not found'}
    # An agent installed later gets hooks on the next setup (an update); the configuration is never modified.
    before = config.read_bytes()
    cli.env['PATH'] = fake_agents(tmp_path, 'claude', 'codex')
    result = json.loads(cli('setup').stdout)
    assert config.read_bytes() == before
    assert result['agents']['claude'] == {'command': str(tmp_path / 'fake-bin/claude'), 'found': True}
    assert result['hooks']['claude']['status'] == 'installed' and result['hooks']['codex']['status'] == 'already installed'
    # A configured command outside PATH counts as found; a dangling one does not.
    cli.env['PATH'] = fake_agents(tmp_path)
    elsewhere = tmp_path / 'elsewhere/codex'
    elsewhere.parent.mkdir()
    elsewhere.write_text('#!/bin/sh\nexit 0\n')
    elsewhere.chmod(0o755)
    cli.env['CODEX_COMMAND'] = str(elsewhere)
    cli.env['CLAUDE_COMMAND'] = str(tmp_path / 'missing/claude')
    result = json.loads(cli('setup').stdout)
    assert result['agents'] == {'claude': {'command': str(tmp_path / 'missing/claude'), 'found': False},
                                'codex': {'command': str(elsewhere), 'found': True}}


def test_setup_hook_failure_and_opt_out_never_fail_setup(cli, tmp_path):
    cli.env['PATH'] = fake_agents(tmp_path, 'claude', 'codex')
    settings_file = tmp_path / 'user/.claude/settings.json'
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text('{not json')
    result = json.loads(cli('setup').stdout)
    assert result['hooks']['claude']['status'].startswith('failed: ') and settings_file.read_text() == '{not json'
    assert result['hooks']['codex']['status'] == 'installed'
    assert not (cli.home / 'data/claude-activity.hooks.json').exists()
    (cli.home / 'data/codex-activity.hooks.json').write_text('null')
    result = json.loads(cli('setup').stdout)
    assert result['hooks']['codex']['status'].startswith('failed: unreadable manifest ')
    settings_file.write_text('{}')
    skipped = {'codex': {'status': 'skipped: ACP_ACTIVITY_HOOKS=0'}, 'claude': {'status': 'skipped: ACP_ACTIVITY_HOOKS=0'}}
    cli.env['ACP_ACTIVITY_HOOKS'] = '0'
    assert json.loads(cli('setup').stdout)['hooks'] == skipped
    del cli.env['ACP_ACTIVITY_HOOKS']
    config = cli.home / 'config.env'
    config.write_text(config.read_text() + 'ACP_ACTIVITY_HOOKS=0\n')
    assert json.loads(cli('setup').stdout)['hooks'] == skipped
    assert settings_file.read_text() == '{}'


@pytest.mark.parametrize('name, event, option, file', [
    ('codex', 'Interrupt', '--codex-home', 'hooks.json'),
    ('claude', 'StopFailure', '--claude-config-dir', 'settings.json')])
def test_setup_refreshes_installed_hooks_at_their_recorded_home(cli, tmp_path, name, event, option, file):
    cli.env['PATH'] = fake_agents(tmp_path)
    cli('setup')
    custom = tmp_path / 'custom-home'
    cli('activity', 'install', '--target', name, option, str(custom))
    hooks_file = custom / file
    data = json.loads(hooks_file.read_text())
    del data['hooks'][event]  # As left by a release that knew fewer events.
    hooks_file.write_text(json.dumps(data))
    trust = True if name == 'codex' else None
    result = json.loads(cli('setup').stdout)['hooks'][name]
    assert result['status'] == 'updated' and result['home'] == str(custom) and result.get('trust_required') is trust
    assert 'note' not in result  # Not on PATH: nothing says where the agent would look.
    assert event in json.loads(hooks_file.read_text())['hooks']
    assert not (tmp_path / 'user').exists()
    cli.env['PATH'] = fake_agents(tmp_path, name)
    result = json.loads(cli('setup').stdout)['hooks'][name]
    assert result['status'] == 'already installed' and str(tmp_path / 'user' / f'.{name}') in result['note']
    assert not (tmp_path / 'user').exists()
    # A command from an earlier install location is replaced, which Codex must trust again.
    manifest = cli.home / 'data' / f'{name}-activity.hooks.json'
    for path in (manifest, hooks_file):
        path.write_text(path.read_text().replace(sys.executable, '/old/python'))
    result = json.loads(cli('setup').stdout)['hooks'][name]
    assert result['status'] == 'updated' and result.get('trust_required') is trust
    text = hooks_file.read_text()
    assert '/old/python' not in text and sys.executable in text


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
    blocked = wait_task(cli, task_id, 'waiting_on_input')
    cli('answer', task_id, 'Use a greeting', '--blocker-id', blocked['blocker_comment_id'])
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
