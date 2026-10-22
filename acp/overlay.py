"""Build and control the macOS Comet companion. The ACP service remains independent."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import plistlib
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

from .config import Settings

SOURCE = Path(__file__).parent / 'native' / 'Leo.swift'


def local_url(value: str) -> str:
    parsed = urlparse(value)
    try:
        valid = (parsed.scheme == 'http' and parsed.hostname in {'127.0.0.1', 'localhost', '::1'}
                 and parsed.port and not parsed.username and not parsed.password
                 and parsed.path in {'', '/'} and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError('Comet requires a local ACP origin: http://127.0.0.1:<port>')
    return value.rstrip('/')


def directory(settings: Settings) -> Path:
    folder = settings.data_dir / 'overlay'
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    return folder


def status(settings: Settings) -> dict:
    folder = directory(settings)
    with (folder / 'panel.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            try:
                state = json.loads((folder / 'state.json').read_text())
                if not isinstance(state, dict):
                    state = {}
            except (OSError, ValueError):
                state = {}
            return {**state, 'running': True, 'log': str(folder / 'leo.log')}
    return {'running': False, 'log': str(folder / 'leo.log')}


def executable_path(settings: Settings) -> Path:
    return directory(settings) / 'Leo.app' / 'Contents' / 'MacOS' / 'Leo'


def developer_tools_missing() -> bool:
    """/usr/bin/swiftc exists on every Mac as an Apple shim; without the command line tools,
    running it opens the install dialog and fails. xcode-select answers quietly instead."""
    try:
        return subprocess.run(['xcode-select', '-p'], capture_output=True).returncode != 0
    except OSError:
        return True


def build(settings: Settings) -> dict:
    """Compile Comet when its source changed. Where it cannot be built, say why instead of failing,
    so installers and `acp start` carry on without it; a failed compile still raises."""
    if sys.platform != 'darwin':
        return {'built': False, 'skipped': 'Comet is a native macOS overlay; nothing to build on this system.'}
    folder = directory(settings)
    executable = executable_path(settings)
    stamp = folder / 'build.sha256'
    digest = hashlib.sha256(SOURCE.read_bytes()).hexdigest()
    with (folder / 'build.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if executable.is_file() and stamp.exists() and stamp.read_text() == digest:
            return {'built': False, 'executable': str(executable)}
        if developer_tools_missing() or not shutil.which('swiftc'):
            return {'built': False, 'skipped': 'Comet was not built: the Apple command line tools are missing. '
                    'Install them (xcode-select --install), then run acp start or acp overlay build.'}
        compile_app(shutil.which('swiftc'), folder / 'Leo.app')
        stamp.write_text(digest)
    return {'built': True, 'executable': str(executable)}


def compile_app(compiler: str, app: Path) -> None:
    executable = app / 'Contents' / 'MacOS' / 'Leo'
    executable.parent.mkdir(parents=True, exist_ok=True)
    candidate = executable.with_suffix('.building')
    try:
        result = subprocess.run([compiler, str(SOURCE), '-o', str(candidate), '-O',
                                 '-framework', 'AppKit', '-framework', 'WebKit'],
                                capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        raise ValueError('Building Comet timed out. Check your Apple command line tools and retry.')
    if result.returncode:
        raise ValueError('Comet could not compile. Install Apple command line tools if needed.\n' + result.stderr[-6000:])
    info = {'CFBundleExecutable': 'Leo', 'CFBundleIdentifier': 'com.acp.leo',
            'CFBundleName': 'ACP Comet', 'CFBundleDisplayName': 'ACP Comet', 'CFBundlePackageType': 'APPL',
            'LSUIElement': True, 'NSHighResolutionCapable': True,
            'NSAppTransportSecurity': {'NSAllowsLocalNetworking': True}}
    (app / 'Contents' / 'Info.plist').write_bytes(plistlib.dumps(info))
    candidate.replace(executable)


def control(settings: Settings, action: str) -> dict:
    if action not in {'show', 'hide', 'toggle', 'reset', 'quit'}:
        raise ValueError('Unknown Comet action')
    current = status(settings)
    if not current['running']:
        if action == 'quit':
            return current
        raise ValueError('Comet is not running. Use acp overlay start.')
    executable = executable_path(settings)
    request_id = secrets.token_hex(16)
    try:
        result = subprocess.run([str(executable), '--control', str(directory(settings)), action, request_id],
                                capture_output=True, text=True, timeout=5)
    except subprocess.TimeoutExpired:
        raise ValueError('Comet did not answer within 5 seconds. Check ' + str(directory(settings) / 'leo.log'))
    if result.returncode:
        raise ValueError('Could not contact Comet: ' + result.stderr[-1000:])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        current = status(settings)
        if (action == 'quit' and not current['running']) or current.get('request_id') == request_id:
            return current
        time.sleep(.05)
    raise ValueError('Comet did not acknowledge the command. Check ' + str(directory(settings) / 'leo.log'))


def start(settings: Settings, url: str = '') -> dict:
    if sys.platform != 'darwin':
        raise ValueError('Comet is a native macOS overlay.')
    folder = directory(settings)
    with (folder / 'start.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if status(settings)['running']:
            return control(settings, 'show')
        from . import daemon
        origin = local_url(url or daemon.status(settings).get('url') or settings.base_url)
        try:
            with httpx.Client(timeout=3, trust_env=False) as client:
                for route in ['/overlay', '/api/activity']:
                    response = client.get(origin + route)
                    response.raise_for_status()
        except httpx.HTTPError:
            raise ValueError('Start ACP from this version first; Comet needs /overlay and /api/activity at ' + origin)
        built = build(settings)
        if built.get('skipped'):
            raise ValueError(built['skipped'])
        executable = Path(built['executable'])
        (folder / 'state.json').unlink(missing_ok=True)
        token_file = folder / 'control.token'
        with open(token_file, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as file:
            file.write(secrets.token_hex(32))
        # A shell's DYLD_LIBRARY_PATH (e.g. /opt/homebrew/lib) replaces the system image codecs
        # AppKit uses to draw the menu bar emoji, and Comet crashes before it can log anything.
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(('LINEAR_', 'DYLD_')) and key not in {'GITHUB_TOKEN', 'GITHUB_WEBHOOK_SECRET'}}
        with open(folder / 'leo.log', 'ab', opener=lambda p, flags: os.open(p, flags, 0o600)) as log:
            process = subprocess.Popen([str(executable), str(folder), origin], env=environment,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       cwd=folder, start_new_session=True, close_fds=True)
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            current = status(settings)
            if current['running'] and current.get('loaded'):
                return control(settings, 'show')
            if process.poll() is not None:
                raise ValueError('Comet exited during startup. Check ' + str(folder / 'leo.log'))
            time.sleep(.1)
        # A slow WebKit startup can remain usable from the menu bar; report it explicitly.
        raise ValueError('Comet did not finish loading. Check acp overlay status and ' + str(folder / 'leo.log'))


def stop(settings: Settings) -> dict:
    return control(settings, 'quit')
