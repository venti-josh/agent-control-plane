"""`acp update`: verify before stopping anything, never interrupt CLI runs, restart only what ran."""
import hashlib
import json
import subprocess
import sys

import httpx
import pytest


@pytest.fixture
def tools(tmp_path, monkeypatch):
    """Fake uv and acp executables that log every call; nothing touches the real install."""
    log, bin_dir, tool_dir = tmp_path / 'calls.jsonl', tmp_path / 'bin', tmp_path / 'tools'
    bin_dir.mkdir()
    (tool_dir / 'acp' / 'bin').mkdir(parents=True)
    state = tmp_path / 'fake.json'
    state.write_text(json.dumps({'before': '0.1.4', 'version': '0.1.6', 'installed': False, 'install_fails': False}))
    prelude = (f'#!{sys.executable}\nimport json, os, sys\n'
               f'state = json.load(open({str(state)!r}))\n'
               f'open({str(log)!r}, "a").write(json.dumps({{"tool": TOOL, "args": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")\n')
    uv = tmp_path / 'uv'
    uv.write_text(prelude.replace('TOOL', '"uv"') +
                  f'if sys.argv[1:] == ["tool", "dir", "--bin"]: print({str(bin_dir)!r})\n'
                  f'elif sys.argv[1:] == ["tool", "dir"]: print({str(tool_dir)!r})\n'
                  'elif sys.argv[1] == "export": print("fastapi==1.0")\n'
                  'elif sys.argv[1:3] == ["tool", "install"] and state["install_fails"]:\n'
                  '    print("network down", file=sys.stderr); sys.exit(2)\n'
                  'elif sys.argv[1:3] == ["tool", "install"]:\n'
                  f'    json.dump({{**state, "installed": True}}, open({str(state)!r}, "w"))\n')
    acp = bin_dir / 'acp'
    # The new setup reports its hooks from 0.1.6 on; an older release's setup prints something else.
    acp.write_text(prelude.replace('TOOL', '"acp"') +
                   'if sys.argv[1:] == ["setup"] and state["version"] >= "0.1.6":\n'
                   '    print(json.dumps({"hooks": {"codex": {"status": "failed: Invalid Codex hooks.json; left unchanged"},'
                   ' "claude": {"status": "updated"}}}))\n'
                   'elif sys.argv[1:] == ["setup"]: print("Configured.")\n'
                   'else: print("{}")\n')
    python = tool_dir / 'acp' / 'bin' / 'python'
    python.write_text(prelude.replace('TOOL', '"python"') + 'print(state["version" if state["installed"] else "before"])\n')
    for path in (uv, acp, python):
        path.chmod(0o755)

    events = []
    service = {'running': True, 'url': 'http://127.0.0.1:8777'}
    panel = {'running': True}
    monkeypatch.setattr('acp.update.find_uv', lambda: str(uv))
    monkeypatch.setattr('acp.update.__version__', '9.9.9')
    monkeypatch.setattr('acp.update.daemon.status', lambda settings: dict(service))
    monkeypatch.setattr('acp.update.daemon.stop', lambda settings: events.append('service.stop'))
    monkeypatch.setattr('acp.update.overlay_running', lambda settings: panel['running'])
    monkeypatch.setattr('acp.update.overlay.stop', lambda settings: events.append('overlay.stop'))

    class Tools:
        def calls(self):
            return [json.loads(line) for line in log.read_text().splitlines()] if log.exists() else []

        def changes(self):
            """Calls that change the installation or a process, excluding version queries."""
            return [c for c in self.calls() if c['tool'] == 'acp' or c['args'][:2] == ['tool', 'install']]

        def set(self, **values):
            state.write_text(json.dumps({**json.loads(state.read_text()), **values}))
    tools = Tools()
    tools.events, tools.service, tools.panel = events, service, panel
    return tools


def release(version='0.1.6', corrupt=False):
    wheel, constraints = b'wheel-' + version.encode(), b'fastapi==1.0\n'
    sums = ''.join(f'{hashlib.sha256(data).hexdigest()}  {name}\n' for name, data in
                   [(f'acp-{version}-py3-none-any.whl', wheel), ('constraints.txt', constraints)])
    assets = {'VERSION': version.encode() + b'\n', f'acp-{version}-py3-none-any.whl': b'tampered' if corrupt else wheel,
              'constraints.txt': constraints, 'SHA256SUMS': sums.encode()}
    requested = []

    def handler(request):
        requested.append(str(request.url))
        name = request.url.path.rsplit('/', 1)[-1]
        return httpx.Response(200, content=assets[name]) if name in assets else httpx.Response(404)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    client.requested = requested
    return client


def test_release_update_verifies_then_stops_installs_and_restarts_what_ran(settings, tools):
    from acp.update import update
    client = release()
    result = update(settings, client=client)
    assert client.requested[0].endswith('/releases/latest/download/VERSION')
    assert any('/releases/download/v0.1.6/acp-0.1.6-py3-none-any.whl' in url for url in client.requested)
    assert result['updated'] is True and result['previous_version'] == '0.1.4'
    assert result['installed_version'] == '0.1.6' and result['restarted'] == {'service': True, 'overlay': True}
    assert tools.events == ['overlay.stop', 'service.stop']
    query = [('uv', ['tool', 'dir']), ('python', ['-I', '-c', 'import acp; print(acp.__version__)'])]
    commands = [(c['tool'], c['args']) for c in tools.calls()]
    assert commands[:2] == query  # the installed version, asked before anything changes
    install = commands[2]
    assert install[0] == 'uv' and install[1][:5] == ['tool', 'install', '--python', '3.14', '--reinstall']
    assert install[1][6].endswith('constraints.txt') and install[1][7].endswith('acp-0.1.6-py3-none-any.whl')
    assert commands[3:] == [('uv', ['tool', 'dir', '--bin']), ('acp', ['setup'])] + query + \
        [('acp', ['start', '--port', '8777']), ('acp', ['overlay', 'start'])]
    # The new setup's hook report rides along; a hook failure is a warning, not an update failure.
    assert result['hooks'] == {'codex': {'status': 'failed: Invalid Codex hooks.json; left unchanged'},
                               'claude': {'status': 'updated'}}
    assert result['warnings'] == ['codex hooks failed: Invalid Codex hooks.json; left unchanged']


def test_current_install_changes_nothing_and_check_has_no_side_effects(settings, tools):
    from acp.update import update
    result = update(settings, check=True, client=release())
    assert result['update_available'] is True and 'updated' not in result
    tools.set(version='0.1.4')
    result = update(settings, client=release('0.1.4'))
    assert result['updated'] is False and '--reinstall' in result['message']
    assert update(settings, check=True, client=release('0.1.4'))['update_available'] is False
    assert tools.events == [] and tools.changes() == []
    result = update(settings, reinstall=True, client=release('0.1.4'))
    assert result['updated'] is True and result['installed_version'] == '0.1.4'


def test_explicit_version_installs_that_release_even_when_older(settings, tools):
    from acp.update import update
    tools.set(version='0.1.3')
    client = release('0.1.3')
    result = update(settings, version='0.1.3', client=client)
    assert result['updated'] is True and result['installed_version'] == '0.1.3'
    assert result['hooks'] is None and result['warnings'] == []
    assert not any(url.endswith('/latest/download/VERSION') for url in client.requested)


def test_checksum_mismatch_or_bad_version_stops_nothing(settings, tools):
    from acp.update import update
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        update(settings, client=release(corrupt=True))
    for bad in ['latest', '1.2', '0.1.6; rm -rf /']:
        with pytest.raises(ValueError):
            update(settings, version=bad, client=release())
    with pytest.raises(ValueError):
        update(settings, client=release('not-a-version'))
    assert tools.events == [] and tools.changes() == []


def test_running_cli_runs_block_the_restart_but_app_and_queued_runs_do_not(settings, service, tools):
    from acp.update import active_cli_runs, update
    service.create_task('Queued', state='todo')
    assert active_cli_runs(settings) == 0
    run = service.claim_next()
    assert active_cli_runs(settings) == 1
    with pytest.raises(RuntimeError, match='1 board run would be interrupted'):
        update(settings, client=release())
    assert tools.events == [] and tools.changes() == []
    with service.store.transaction() as db:  # A Codex app run lives in the app and survives restarts.
        db.execute('INSERT INTO desktop_runs(run_id, phase) VALUES(?, ?)', (run['id'], 'dispatched'))
    assert active_cli_runs(settings) == 0
    with service.store.transaction() as db:
        db.execute('DELETE FROM desktop_runs')
    result = update(settings, force=True, client=release())
    assert result['updated'] is True


def test_run_check_reads_a_wal_database_whose_side_files_are_gone(settings, service):
    # A stopped or restarting service can leave no -wal/-shm files; a mode=ro open then fails.
    import sqlite3
    from acp.update import active_cli_runs
    service.create_task('Running', state='todo')
    service.claim_next()
    db = sqlite3.connect(settings.db_path)
    assert db.execute('PRAGMA journal_mode').fetchone()[0] == 'wal'
    db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
    db.close()
    for suffix in ('-wal', '-shm'):
        settings.db_path.with_name(settings.db_path.name + suffix).unlink(missing_ok=True)
    assert active_cli_runs(settings) == 1


def test_source_install_exports_the_checkout_lock_and_restarts_only_what_ran(settings, tools, tmp_path):
    from acp.update import update
    source = tmp_path / 'checkout'
    source.mkdir()
    (source / 'pyproject.toml').write_text('[project]\nname = "acp"\nversion = "0.1.4"\n')
    tools.set(version='0.1.4')
    tools.service['running'], tools.panel['running'] = False, False
    result = update(settings, source=source, client=release())
    assert result['updated'] is True and result['source'] == str(source.resolve())
    assert result['restarted'] == {'service': False, 'overlay': False} and tools.events == []
    export, install = [c for c in tools.calls() if c['args'][:1] == ['export'] or c['args'][:2] == ['tool', 'install']]
    assert export['args'][0] == 'export' and '--locked' in export['args'] and export['cwd'] == str(source.resolve())
    assert install['args'][-1] == str(source.resolve())
    assert not any(c['args'][:1] == ['start'] for c in tools.calls())
    (source / 'pyproject.toml').write_text('[project]\nname = "other"\nversion = "1.0.0"\n')
    with pytest.raises(ValueError, match='not an ACP checkout'):
        update(settings, source=source, client=release())


def test_compares_against_the_installed_tool_not_the_code_running_the_update(settings, tools, monkeypatch):
    # A checkout's `uv run acp update` runs newer code than the installed tool it replaces.
    from acp.update import update
    monkeypatch.setattr('acp.update.__version__', '0.1.6')
    assert update(settings, check=True, client=release())['installed_version'] == '0.1.4'
    result = update(settings, client=release())
    assert result['updated'] is True and result['previous_version'] == '0.1.4'
    assert result['installed_version'] == '0.1.6'


def test_failed_install_says_acp_is_stopped(settings, tools):
    from acp.update import update
    tools.set(install_fails=True)
    with pytest.raises(RuntimeError, match='ACP is stopped') as error:
        update(settings, client=release())
    assert 'network down' in str(error.value)
    assert not any(c['tool'] == 'acp' for c in tools.calls())


def test_cli_reports_version_and_update_options():
    from acp import __version__
    result = subprocess.run([sys.executable, '-m', 'acp.cli', '--version'], capture_output=True, text=True, timeout=30)
    assert result.returncode == 0 and result.stdout.strip() == f'acp {__version__}'
    result = subprocess.run([sys.executable, '-m', 'acp.cli', 'update', '--help'], capture_output=True, text=True, timeout=30)
    assert all(flag in result.stdout for flag in ['--version', '--source', '--check', '--reinstall', '--force'])
