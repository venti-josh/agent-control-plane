"""Update this ACP installation in place from a published release or a local checkout."""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from urllib.parse import urlparse

import httpx

from . import __version__, daemon, overlay
from .config import Settings, user_home

REPOSITORY = 'https://github.com/venti-josh/agent-control-plane'
VERSION = re.compile(r'\d+\.\d+\.\d+')


def parse_version(value: str) -> tuple[int, ...]:
    if not isinstance(value, str) or not VERSION.fullmatch(value):
        raise ValueError(f'Invalid ACP release version {value!r}; expected X.Y.Z.')
    return tuple(int(part) for part in value.split('.'))


def find_uv() -> str:
    # The release installer bootstraps its own uv when none is on PATH.
    for candidate in (shutil.which('uv'), str(user_home() / 'installer/uv/uv')):
        if candidate and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError('uv is required to update ACP; rerun the release installer to install it.')


def release_base(version: str | None = None) -> str:
    if os.getenv('ACP_RELEASE_BASE_URL'):
        return os.environ['ACP_RELEASE_BASE_URL'].rstrip('/')
    return f'{REPOSITORY}/releases/' + (f'download/v{version}' if version else 'latest/download')


def latest_version(client: httpx.Client) -> str:
    response = client.get(release_base() + '/VERSION')
    response.raise_for_status()
    version = response.text.strip()
    parse_version(version)
    return version


def download_release(client: httpx.Client, version: str, folder: Path) -> tuple[Path, Path]:
    """Fetch and verify the same assets the release installer uses."""
    wheel = f'acp-{version}-py3-none-any.whl'
    for name in (wheel, 'constraints.txt', 'SHA256SUMS'):
        response = client.get(f'{release_base(version)}/{name}')
        response.raise_for_status()
        (folder / name).write_bytes(response.content)
    sums = {}
    for line in (folder / 'SHA256SUMS').read_text().splitlines():
        fields = line.split()
        if len(fields) == 2:
            sums[fields[1].lstrip('*')] = fields[0]
    for name in (wheel, 'constraints.txt'):
        actual = hashlib.sha256((folder / name).read_bytes()).hexdigest()
        if len(sums.get(name, '')) != 64 or sums[name] != actual:
            raise RuntimeError(f'Release checksum mismatch: {name}. Nothing was changed.')
    return folder / wheel, folder / 'constraints.txt'


def checkout(source: Path, uv: str, folder: Path) -> tuple[str, Path]:
    """Read a local checkout's version and export its locked runtime constraints."""
    source = Path(source).expanduser().resolve()
    try:
        project = tomllib.loads((source / 'pyproject.toml').read_text())['project']
    except (OSError, KeyError, tomllib.TOMLDecodeError):
        raise ValueError(f'{source} is not an ACP checkout (no readable pyproject.toml).') from None
    if project.get('name') != 'acp':
        raise ValueError(f'{source} is not an ACP checkout.')
    parse_version(project.get('version', ''))
    export = subprocess.run([uv, 'export', '--locked', '--no-dev', '--no-hashes', '--no-emit-project',
                             '--format', 'requirements-txt'], cwd=source, capture_output=True, text=True)
    if export.returncode:
        raise RuntimeError('uv could not export locked constraints from the checkout: '
                           + (export.stderr.strip().splitlines() or ['unknown error'])[-1])
    constraints = folder / 'constraints.txt'
    constraints.write_text(export.stdout)
    return project['version'], constraints


def active_cli_runs(settings: Settings) -> int:
    """Board runs a restart would interrupt; queued runs wait."""
    path = settings.db_path
    if not path.exists():
        return 0
    try:
        # Read only, but not mode=ro: a read-only connection cannot recreate a WAL database's
        # -wal/-shm files, which are absent around service restarts. mode=rw never creates the file.
        db = sqlite3.connect(f'file:{path}?mode=rw', uri=True, timeout=5)
        try:
            return db.execute("SELECT count(*) FROM runs WHERE status='running'").fetchone()[0]
        finally:
            db.close()
    except sqlite3.Error as exc:
        raise RuntimeError(f'Cannot check {path} for active board runs: {exc}') from exc


def overlay_running(settings: Settings) -> bool:
    return sys.platform == 'darwin' and overlay.status(settings)['running']


def run(command: list[str], failure: str) -> str:
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        detail = (result.stderr.strip() or result.stdout.strip()).splitlines()[-3:]
        raise RuntimeError(failure + (': ' + ' '.join(detail) if detail else ''))
    return result.stdout.strip()


def installed_version(uv: str) -> str | None:
    """Ask the uv tool environment itself: it can differ from the code running this command
    (a checkout's `uv run acp update`), and releases before 0.1.5 have no --version flag."""
    try:
        python = Path(run([uv, 'tool', 'dir'], 'uv tool dir failed')) / 'acp' / 'bin' / 'python'
        if not python.exists():
            return None
        return run([str(python), '-I', '-c', 'import acp; print(acp.__version__)'],
                   'The installed ACP does not import').split()[-1]
    except (RuntimeError, IndexError):
        return None


def update(settings: Settings, version: str | None = None, source: Path | None = None,
           check: bool = False, reinstall: bool = False, force: bool = False,
           client: httpx.Client | None = None) -> dict:
    if version and source:
        raise ValueError('Choose either --version or --source.')
    if version:
        parse_version(version)
    owned = client is None
    client = client or httpx.Client(timeout=60, follow_redirects=True)
    try:
        with tempfile.TemporaryDirectory(prefix='acp-update-') as temp:
            folder = Path(temp)
            try:
                uv = find_uv()
            except RuntimeError:
                if source or not check:
                    raise
                uv = None
            current = (installed_version(uv) if uv else None) or __version__
            if source:
                target, constraints = checkout(source, uv, folder)
                package = Path(source).expanduser().resolve()
            else:
                target = version or latest_version(client)
            result = {'installed_version': current, 'target_version': target,
                      'source': str(package) if source else release_base(target)}
            # A checkout can differ from the installed code under the same version number.
            newer = bool(source) or parse_version(target) > parse_version(current)
            if check:
                return {**result, 'update_available': newer}
            if not (newer or reinstall or (version and target != current)):
                return {**result, 'updated': False,
                        'message': f'ACP {current} is current; use --reinstall to install {target} anyway.'}
            busy = active_cli_runs(settings)
            if busy and not force:
                raise RuntimeError(f'{busy} board run{"s" * (busy != 1)} would be interrupted by the restart. '
                                   'Wait for them to finish or cancel them, or pass --force.')
            if not source:
                package, constraints = download_release(client, target, folder)
            # Everything that can fail without side effects has passed; now stop what is running.
            service = daemon.status(settings)
            panel = overlay_running(settings)
            port = urlparse(service.get('url') or '').port
            if panel:
                overlay.stop(settings)
            if service['running']:
                daemon.stop(settings)
            stopped = 'ACP is stopped' if service['running'] else 'ACP was not running'
            run([uv, 'tool', 'install', '--python', '3.14', '--reinstall', '--constraints', str(constraints),
                 str(package)], f'uv tool install failed; {stopped}. Rerun acp update or the release installer')
            # From here on use only the new executable; this process still runs the old code.
            executable = str(Path(run([uv, 'tool', 'dir', '--bin'], 'uv tool dir failed')) / 'acp')
            run([executable, 'setup'], 'acp setup failed after installing')
            installed = installed_version(uv)
            if installed is None:
                raise RuntimeError('Installed, but the new ACP does not import; rerun the release installer')
            restarted = {'service': False, 'overlay': False}
            if service['running']:
                run([executable, 'start'] + (['--port', str(port)] if port else []),
                    'Updated, but acp start failed; run acp start and inspect acp logs')
                restarted['service'] = True
            if panel:
                run([executable, 'overlay', 'start'], 'Updated, but acp overlay start failed; run it again')
                restarted['overlay'] = True
            return {**result, 'updated': True, 'previous_version': current,
                    'installed_version': installed, 'restarted': restarted}
    finally:
        if owned:
            client.close()
