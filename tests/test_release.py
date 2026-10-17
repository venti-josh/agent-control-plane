"""Exercise release packaging with disposable source and wheel fixtures."""
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def release_tree(tmp_path):
    for directory in ('scripts', 'acp', 'dist/release'):
        (tmp_path / directory).mkdir(parents=True)
    shutil.copyfile(ROOT / 'scripts/release-assets.py', tmp_path / 'scripts/release-assets.py')
    (tmp_path / 'pyproject.toml').write_text('[project]\nversion = "1.2.3"\n')
    (tmp_path / 'acp/__init__.py').write_text('__version__ = "1.2.3"\n')
    (tmp_path / 'dist/acp-1.2.3-py3-none-any.whl').write_bytes(b'wheel fixture')
    (tmp_path / 'dist/constraints.txt').write_text('example==1.0\n')
    (tmp_path / 'scripts/install.sh').write_text("acp_default_version=''\n")
    return tmp_path


def package(root):
    env = {k: v for k, v in os.environ.items() if not k.startswith('GITHUB_REF')}
    return subprocess.run([sys.executable, str(root / 'scripts/release-assets.py')],
                          env=env, text=True, capture_output=True)


def test_release_rejects_mismatched_runtime_version(release_tree):
    (release_tree / 'acp/__init__.py').write_text('__version__ = "1.2.2"\n')
    result = package(release_tree)
    assert result.returncode != 0
    assert 'version' in result.stderr.lower()
    assert not (release_tree / 'dist/release/VERSION').exists()


def test_release_contains_only_current_version_and_valid_checksums(release_tree):
    release = release_tree / 'dist/release'
    (release / 'acp-1.2.2-py3-none-any.whl').write_bytes(b'stale wheel')
    result = package(release_tree)
    assert result.returncode == 0, result.stderr
    assert sorted(p.name for p in release.iterdir()) == [
        'SHA256SUMS', 'VERSION', 'acp-1.2.3-py3-none-any.whl', 'constraints.txt', 'install.sh']
    assert (release / 'VERSION').read_text() == '1.2.3\n'
    assert (release / 'install.sh').read_text() == "acp_default_version='1.2.3'\n"
    for line in (release / 'SHA256SUMS').read_text().splitlines():
        digest, name = line.split('  ')
        assert hashlib.sha256((release / name).read_bytes()).hexdigest() == digest
