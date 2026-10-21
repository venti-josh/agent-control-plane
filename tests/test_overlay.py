"""Native launcher safeguards; AppKit interaction checks live in mac_overlay.py."""
import json
import subprocess
from pathlib import Path

import pytest

from acp import overlay


def test_overlay_url_accepts_only_local_http_service_origins():
    assert overlay.local_url('http://127.0.0.1:8877/') == 'http://127.0.0.1:8877'
    for url in ['https://example.com', 'http://example.com:8765', 'file:///tmp/overlay.html',
                'http://user:secret@127.0.0.1:8765', 'http://127.0.0.1:8765/path',
                'http://127.0.0.1:8765/?remote=true', 'http://127.0.0.1:99999']:
        with pytest.raises(ValueError):
            overlay.local_url(url)


def test_stale_native_status_never_signals_a_pid(settings):
    directory = settings.data_dir / 'overlay'
    directory.mkdir(parents=True)
    (directory / 'state.json').write_text(json.dumps({'pid': 1, 'visible': True}))
    assert overlay.status(settings)['running'] is False
    assert overlay.stop(settings)['running'] is False


def test_overlay_refuses_non_macos_without_compiling(settings, monkeypatch):
    monkeypatch.setattr(overlay.sys, 'platform', 'linux')
    with pytest.raises(ValueError, match='macOS'):
        overlay.start(settings)
    assert not (settings.data_dir / 'overlay' / 'Leo.app').exists()


def test_native_surface_is_separate_from_board(client):
    board = client.get('/').text
    assert 'id="activity-overlay"' not in board
    assert '/static/activity.js' not in board
    page = client.get('/overlay')
    assert page.status_code == 200
    assert 'id="activity-overlay"' in page.text
    assert '/static/activity.js' in page.text
    assert 'id="board"' not in page.text
    assert client.get('/api/activity').status_code == 200


@pytest.fixture
def swiftc(monkeypatch, tmp_path):
    """A pretend Mac with the command line tools: the compiler records invocations and writes the
    requested output file, and Comet's source is a scratch file so tests can change it."""
    monkeypatch.setattr(overlay.sys, 'platform', 'darwin')
    monkeypatch.setattr(overlay, 'developer_tools_missing', lambda: False)
    monkeypatch.setattr(overlay.shutil, 'which', lambda name: '/usr/bin/swiftc' if name == 'swiftc' else None)
    source = tmp_path / 'Leo.swift'
    source.write_text('print(1)')
    monkeypatch.setattr(overlay, 'SOURCE', source)

    class Calls(list):
        returncode = 0
    calls = Calls()
    calls.source = source

    def run(command, **kwargs):
        calls.append(command)
        Path(command[command.index('-o') + 1]).write_text('binary')
        return subprocess.CompletedProcess(command, calls.returncode, '', 'error: boom' if calls.returncode else '')
    monkeypatch.setattr(overlay.subprocess, 'run', run)
    return calls


def test_developer_tools_are_detected_through_xcode_select_not_the_swiftc_shim(monkeypatch):
    for returncode, missing in [(0, False), (2, True)]:
        monkeypatch.setattr(overlay.subprocess, 'run', lambda *a, **k: subprocess.CompletedProcess(a, returncode))
        assert overlay.developer_tools_missing() is missing

    def absent(*args, **kwargs):
        raise FileNotFoundError('xcode-select')
    monkeypatch.setattr(overlay.subprocess, 'run', absent)
    assert overlay.developer_tools_missing() is True


def test_build_skips_with_a_reason_instead_of_failing_where_comet_cannot_be_built(settings, monkeypatch):
    monkeypatch.setattr(overlay.sys, 'platform', 'linux')
    result = overlay.build(settings)
    assert result['built'] is False and 'macOS' in result['skipped']
    monkeypatch.setattr(overlay.sys, 'platform', 'darwin')
    monkeypatch.setattr(overlay, 'developer_tools_missing', lambda: True)
    result = overlay.build(settings)
    assert result['built'] is False and 'xcode-select --install' in result['skipped']
    monkeypatch.setattr(overlay, 'developer_tools_missing', lambda: False)
    monkeypatch.setattr(overlay.shutil, 'which', lambda name: None)
    assert 'xcode-select --install' in overlay.build(settings)['skipped']
    assert not (settings.data_dir / 'overlay' / 'Leo.app').exists()


def test_build_compiles_only_when_the_source_changed(settings, swiftc, monkeypatch):
    first = overlay.build(settings)
    executable = Path(first['executable'])
    assert first['built'] is True and executable.read_text() == 'binary'
    assert swiftc[0][0] == '/usr/bin/swiftc' and str(swiftc.source) in swiftc[0]
    assert (executable.parents[2] / 'Contents' / 'Info.plist').exists()
    assert overlay.build(settings) == {'built': False, 'executable': str(executable)}
    assert len(swiftc) == 1
    swiftc.source.write_text('print(2)')
    assert overlay.build(settings)['built'] is True and len(swiftc) == 2
    # A built Comet stays usable when the command line tools later disappear.
    monkeypatch.setattr(overlay, 'developer_tools_missing', lambda: True)
    assert overlay.build(settings) == {'built': False, 'executable': str(executable)}


def test_build_reports_a_failed_compile(settings, swiftc):
    swiftc.returncode = 1
    with pytest.raises(ValueError, match='could not compile'):
        overlay.build(settings)
    assert not overlay.executable_path(settings).exists()


def test_start_refuses_when_comet_cannot_be_built(settings, monkeypatch):
    monkeypatch.setattr(overlay.sys, 'platform', 'darwin')
    monkeypatch.setattr(overlay, 'developer_tools_missing', lambda: True)

    class Client:  # The service answers; only the build is missing.
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            pass

        def get(self, url):
            return type('Response', (), {'raise_for_status': lambda self: None})()
    monkeypatch.setattr(overlay.httpx, 'Client', Client)
    with pytest.raises(ValueError, match='xcode-select'):
        overlay.start(settings, 'http://127.0.0.1:1')
    assert not (settings.data_dir / 'overlay' / 'Leo.app').exists()


def test_control_reports_an_unresponsive_comet_instead_of_crashing(settings, monkeypatch):
    monkeypatch.setattr(overlay, 'status', lambda s: {'running': True, 'log': 'leo.log'})

    def hang(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 5)
    monkeypatch.setattr(overlay.subprocess, 'run', hang)
    with pytest.raises(ValueError, match='did not answer'):
        overlay.control(settings, 'show')
