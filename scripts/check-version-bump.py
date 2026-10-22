"""Fail unless the working tree's release version is bumped above the base ref's.

Usage: python3 scripts/check-version-bump.py [BASE_REF]   (default origin/main)
"""
import re
import runpy
import subprocess
import sys
import tomllib
from pathlib import Path

root = Path(__file__).resolve().parents[1]


def parse_version(text, source):
    version = tomllib.loads(text)['project']['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise SystemExit(f'{source}: release version must be X.Y.Z, got {version!r}')
    return version


def locked_version(text):
    for package in tomllib.loads(text).get('package', []):
        if package.get('name') == 'acp' and package.get('source', {}).get('editable') == '.':
            return package.get('version')
    return None


def main(base):
    head = parse_version((root / 'pyproject.toml').read_text(), 'pyproject.toml')
    runtime = runpy.run_path(str(root / 'acp/__init__.py'))['__version__']
    if runtime != head:
        raise SystemExit(f'acp/__init__.py has {runtime} but pyproject.toml has {head}; they must match')
    lock = root / 'uv.lock'
    if lock.exists() and locked_version(lock.read_text()) != head:
        raise SystemExit(f'uv.lock does not record acp {head}; run uv lock after bumping the version')
    shown = subprocess.run(['git', 'show', f'{base}:pyproject.toml'], cwd=root, text=True, capture_output=True)
    if shown.returncode:
        raise SystemExit(shown.stderr.strip() or f'cannot read pyproject.toml at {base}')
    base_version = parse_version(shown.stdout, f'{base}:pyproject.toml')
    if tuple(map(int, head.split('.'))) <= tuple(map(int, base_version.split('.'))):
        raise SystemExit(f'Version {head} must be greater than {base_version} on {base}: every merge '
                         'publishes a release, so bump pyproject.toml and acp/__init__.py and run uv lock')
    print(f'Version bump {base_version} -> {head}')


if __name__ == '__main__':
    main(sys.argv[1] if len(sys.argv) > 1 else 'origin/main')
