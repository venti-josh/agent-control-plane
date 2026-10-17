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


def build(settings: Settings) -> Path:
    if sys.platform != 'darwin':
        raise ValueError('Comet is a native macOS overlay. The ACP board is still available on other systems.')
    folder = directory(settings)
    source = Path(__file__).parent / 'native' / 'Leo.swift'
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    app = folder / 'Leo.app'
    executable = app / 'Contents' / 'MacOS' / 'Leo'
    stamp = folder / 'build.sha256'
    if executable.is_file() and stamp.exists() and stamp.read_text() == digest:
        return executable
    compiler = shutil.which('swiftc')
    if not compiler:
        raise ValueError('Install the Apple command line tools (xcode-select --install) to build Comet once.')
    executable.parent.mkdir(parents=True, exist_ok=True)
    candidate = executable.with_suffix('.building')
    try:
        result = subprocess.run([compiler, str(source), '-o', str(candidate), '-O',
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
    stamp.write_text(digest)
    return executable


def control(settings: Settings, action: str) -> dict:
    if action not in {'show', 'hide', 'toggle', 'reset', 'quit'}:
        raise ValueError('Unknown Comet action')
    current = status(settings)
    if not current['running']:
        if action == 'quit':
            return current
        raise ValueError('Comet is not running. Use acp overlay start.')
    executable = directory(settings) / 'Leo.app' / 'Contents' / 'MacOS' / 'Leo'
    request_id = secrets.token_hex(16)
    result = subprocess.run([str(executable), '--control', str(directory(settings)), action, request_id],
                            capture_output=True, text=True, timeout=5)
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
        executable = build(settings)
        (folder / 'state.json').unlink(missing_ok=True)
        token_file = folder / 'control.token'
        with open(token_file, 'w', opener=lambda p, flags: os.open(p, flags, 0o600)) as file:
            file.write(secrets.token_hex(32))
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith('LINEAR_') and key not in {'GITHUB_TOKEN', 'GITHUB_WEBHOOK_SECRET'}}
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
