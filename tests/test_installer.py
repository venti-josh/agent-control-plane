"""Run the installer against local release fixtures; never execute a damaged wheel."""
import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize('damage', ['wheel', 'constraints', 'version'])
def test_installer_rejects_invalid_release_before_install(tmp_path, damage):
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
