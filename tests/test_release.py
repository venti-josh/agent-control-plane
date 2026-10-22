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


@pytest.fixture
def bump_tree(tmp_path):
    for directory in ('scripts', 'acp'):
        (tmp_path / directory).mkdir()
    shutil.copyfile(ROOT / 'scripts/check-version-bump.py', tmp_path / 'scripts/check-version-bump.py')
    write_versions(tmp_path, '1.2.3')
    git = ['git', '-c', 'user.name=t', '-c', 'user.email=t@example.com', '-c', 'commit.gpgsign=false']
    subprocess.run([*git, 'init', '-q', '-b', 'main'], cwd=tmp_path, check=True)
    subprocess.run([*git, 'add', '-A'], cwd=tmp_path, check=True)
    subprocess.run([*git, 'commit', '-q', '-m', 'base'], cwd=tmp_path, check=True)
    return tmp_path


def write_versions(root, version, runtime=None, locked=None):
    (root / 'pyproject.toml').write_text(f'[project]\nname = "acp"\nversion = "{version}"\n')
    (root / 'acp/__init__.py').write_text(f'__version__ = "{runtime or version}"\n')
    (root / 'uv.lock').write_text(
        f'version = 1\n\n[[package]]\nname = "acp"\nversion = "{locked or version}"\n'
        'source = { editable = "." }\n')


def check_bump(root):
    return subprocess.run([sys.executable, str(root / 'scripts/check-version-bump.py'), 'main'],
                          cwd=root, text=True, capture_output=True)


def test_bump_accepts_a_greater_version(bump_tree):
    write_versions(bump_tree, '1.2.4')
    result = check_bump(bump_tree)
    assert result.returncode == 0, result.stderr
    assert '1.2.3 -> 1.2.4' in result.stdout


def test_bump_accepts_a_greater_minor_with_lower_patch(bump_tree):
    write_versions(bump_tree, '1.3.0')
    assert check_bump(bump_tree).returncode == 0


@pytest.mark.parametrize('version', ['1.2.3', '1.2.2', '0.9.9'])
def test_bump_rejects_unchanged_or_lower_versions(bump_tree, version):
    write_versions(bump_tree, version)
    result = check_bump(bump_tree)
    assert result.returncode != 0
    assert f'Version {version} must be greater than 1.2.3' in result.stderr


def test_bump_rejects_mismatched_runtime_version(bump_tree):
    write_versions(bump_tree, '1.2.4', runtime='1.2.3')
    result = check_bump(bump_tree)
    assert result.returncode != 0
    assert 'must match' in result.stderr


def test_bump_rejects_stale_lockfile(bump_tree):
    write_versions(bump_tree, '1.2.4', locked='1.2.3')
    result = check_bump(bump_tree)
    assert result.returncode != 0
    assert 'uv lock' in result.stderr


def test_bump_rejects_malformed_version(bump_tree):
    write_versions(bump_tree, '1.2.4rc1')
    result = check_bump(bump_tree)
    assert result.returncode != 0
    assert 'X.Y.Z' in result.stderr
