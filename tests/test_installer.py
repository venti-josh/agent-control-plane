"""Run the installer against local release fixtures; never execute a damaged wheel."""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from acp import overlay

ROOT = Path(__file__).resolve().parents[1]


def release_assets(tmp_path):
    release = tmp_path / 'release'
    release.mkdir()
    (release / 'VERSION').write_text('0.1.0\n')
    wheel = release / 'acp-0.1.0-py3-none-any.whl'
    wheel.write_bytes(b'fixture-wheel')
    constraints = release / 'constraints.txt'
    constraints.write_text('httpx==0.28.1\n')
    (release / 'SHA256SUMS').write_text(''.join(
        hashlib.sha256(p.read_bytes()).hexdigest() + '  ' + p.name + '\n'
        for p in (wheel, constraints)))
    return release, wheel, constraints


@pytest.mark.parametrize('damage', ['wheel', 'constraints', 'version'])
def test_installer_rejects_invalid_release_before_install(tmp_path, damage):
    release, wheel, constraints = release_assets(tmp_path)
    if damage == 'version':
        (release / 'VERSION').write_text('../../escape\n')
    else:
        (wheel if damage == 'wheel' else constraints).write_text('damaged')
    env = {**os.environ, 'ACP_RELEASE_BASE_URL': release.as_uri(),
           'UV_TOOL_DIR': str(tmp_path / 'tools'), 'UV_TOOL_BIN_DIR': str(tmp_path / 'bin'),
           'ACP_HOME': str(tmp_path / 'home'), 'HOME': str(tmp_path / 'user'),  # setup must never reach
           'CLAUDE_CONFIG_DIR': str(tmp_path / 'user/.claude'), 'CODEX_HOME': str(tmp_path / 'user/.codex')}
    result = subprocess.run(['sh', str(ROOT / 'scripts/install.sh')], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert 'checksum' in result.stderr.lower() or 'version' in result.stderr.lower()
    assert not (tmp_path / 'bin/acp').exists()


SCENARIOS = ['built', 'no-swiftc', 'compile-failed', 'opted-out', 'linux', 'service-running', 'comet-running']


@pytest.mark.parametrize('scenario', SCENARIOS)
def test_installer_builds_comet_on_macos_and_never_fails_over_it(tmp_path, scenario, settings, monkeypatch):
    """Fake uv, acp and uname stand in for the real tools; the installer logic itself runs for real."""
    release = release_assets(tmp_path)[0]
    code, output = 1 if scenario == 'compile-failed' else 0, ''
    if scenario == 'no-swiftc':  # The skip text comes from the real build() so the sed is tested against it.
        monkeypatch.setattr(overlay.sys, 'platform', 'darwin')
        monkeypatch.setattr(overlay, 'developer_tools_missing', lambda: True)
        output = json.dumps(overlay.build(settings), indent=2) + '\n'
    elif scenario != 'compile-failed':
        output = json.dumps({'built': True, 'executable': '/tmp/Leo.app/Contents/MacOS/Leo'}, indent=2) + '\n'
    (tmp_path / 'build.json').write_text(output)
    (tmp_path / 'status.json').write_text(json.dumps(
        {'running': scenario == 'service-running', 'comet': {'running': scenario == 'comet-running'}}, indent=2))
    fake = tmp_path / 'fake-bin'
    fake.mkdir()
    acp = tmp_path / 'fake-acp'
    acp.write_text('#!/bin/sh\necho "$*" >> "$ACP_CALLS"\ncase "$1 $2" in\n'
                   '"overlay build") cat "$ACP_BUILD"; [ "$ACP_BUILD_EXIT" = 0 ] || printf '
                   '"Comet could not compile. Install Apple command line tools if needed.\\nerror: boom\\n" >&2; '
                   'exit "$ACP_BUILD_EXIT";;\n"status ") cat "$ACP_STATUS";;\n*) echo "{}";;\nesac\n')
    (fake / 'uv').write_text('#!/bin/sh\ncase "$1 $2" in\n"--version ") echo "uv 0.13.1";;\n'
                             '"tool dir") echo "$UV_TOOL_BIN_DIR";;\n'
                             '"tool install") mkdir -p "$UV_TOOL_BIN_DIR"; cp "$FAKE_ACP" "$UV_TOOL_BIN_DIR/acp";;\n'
                             'esac\n')
    (fake / 'uname').write_text('#!/bin/sh\necho %s\n' % ('Linux' if scenario == 'linux' else 'Darwin'))
    for path in (acp, fake / 'uv', fake / 'uname'):
        path.chmod(0o755)
    env = {**os.environ, 'PATH': f'{fake}:{os.environ["PATH"]}', 'ACP_RELEASE_BASE_URL': release.as_uri(),
           'UV_TOOL_DIR': str(tmp_path / 'tools'), 'UV_TOOL_BIN_DIR': str(tmp_path / 'bin'),
           'ACP_HOME': str(tmp_path / 'home'), 'FAKE_ACP': str(acp), 'ACP_CALLS': str(tmp_path / 'calls.log'),
           'ACP_BUILD': str(tmp_path / 'build.json'), 'ACP_BUILD_EXIT': str(code),
           'ACP_STATUS': str(tmp_path / 'status.json')}
    env.pop('ACP_OVERLAY', None)
    if scenario == 'opted-out':
        env['ACP_OVERLAY'] = '0'
    if scenario in {'service-running', 'comet-running'}:  # an earlier install exists
        (tmp_path / 'bin').mkdir()
        (tmp_path / 'bin' / 'acp').write_bytes(acp.read_bytes())
        (tmp_path / 'bin' / 'acp').chmod(0o755)
    result = subprocess.run(['sh', str(ROOT / 'scripts/install.sh')], env=env,
                            capture_output=True, text=True, timeout=30)
    commands = (tmp_path / 'calls.log').read_text().splitlines()
    if scenario == 'service-running':
        assert result.returncode != 0 and 'acp stop' in result.stderr and commands == ['status']
        return
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ACP 0.1.0 installed' in result.stdout
    if scenario == 'comet-running':  # Comet alone running (the service had died) does not block the install
        assert commands == ['status', 'setup', 'overlay build']
        return
    if scenario in {'opted-out', 'linux'}:
        assert commands == ['setup'] and 'Comet' not in result.stdout
        return
    assert commands == ['setup', 'overlay build']
    if scenario == 'built':
        assert 'Comet is built; it opens with acp start' in result.stdout
    elif scenario == 'no-swiftc':
        assert 'Comet: ' + json.loads(output)['skipped'] in result.stdout
    else:
        assert ('Comet could not be built: Comet could not compile. Install Apple command line tools if needed.\n'
                'Run acp overlay build to see the compiler output.') in result.stdout
