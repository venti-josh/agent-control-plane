"""Run the installer against local release fixtures; never execute a damaged wheel."""
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

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
           'ACP_HOME': str(tmp_path / 'home')}
    result = subprocess.run(['sh', str(ROOT / 'scripts/install.sh')], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode != 0
    assert 'checksum' in result.stderr.lower() or 'version' in result.stderr.lower()
    assert not (tmp_path / 'bin/acp').exists()


SKIPPED = ('swiftc is not installed, so Comet was not built. Install the Apple command line tools '
           '(xcode-select --install), then run acp overlay build.')
OUTCOMES = {  # `acp overlay build` exit code and output per scenario; the installer must succeed in all of them
    'built': (0, '{\n  "built": true,\n  "executable": "/tmp/Leo.app/Contents/MacOS/Leo"\n}\n'),
    'no-swiftc': (0, '{\n  "built": false,\n  "skipped": "%s"\n}\n' % SKIPPED),
    'compile-failed': (1, ''), 'opted-out': (0, ''), 'linux': (0, '')}


@pytest.mark.parametrize('scenario', sorted(OUTCOMES))
def test_installer_builds_comet_on_macos_and_never_fails_over_it(tmp_path, scenario):
    """Fake uv, acp and uname stand in for the real tools; the installer logic itself runs for real."""
    release = release_assets(tmp_path)[0]
    code, output = OUTCOMES[scenario]
    (tmp_path / 'build.json').write_text(output)
    fake = tmp_path / 'fake-bin'
    fake.mkdir()
    acp = tmp_path / 'fake-acp'
    acp.write_text('#!/bin/sh\necho "$*" >> "$ACP_CALLS"\n'
                   'if [ "$1 $2" = "overlay build" ]; then cat "$ACP_BUILD"; '
                   '[ "$ACP_BUILD_EXIT" = 0 ] || echo "error: boom" >&2; exit "$ACP_BUILD_EXIT"; fi\necho "{}"\n')
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
           'ACP_BUILD': str(tmp_path / 'build.json'), 'ACP_BUILD_EXIT': str(code)}
    env.pop('ACP_OVERLAY', None)
    if scenario == 'opted-out':
        env['ACP_OVERLAY'] = '0'
    result = subprocess.run(['sh', str(ROOT / 'scripts/install.sh')], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'ACP 0.1.0 installed' in result.stdout
    commands = (tmp_path / 'calls.log').read_text().splitlines()
    if scenario in {'opted-out', 'linux'}:
        assert commands == ['setup'] and 'Comet' not in result.stdout
        return
    assert commands == ['setup', 'overlay build']
    assert {'built': 'Comet is built; it opens with acp start',
            'no-swiftc': 'Comet: ' + SKIPPED,
            'compile-failed': 'Comet could not be built: error: boom'}[scenario] in result.stdout
