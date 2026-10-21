"""User-owned detached service. Control is authenticated; PIDs are informational."""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx
import uvicorn
from dotenv import dotenv_values
from fastapi import HTTPException, Request

from . import claude_activity, overlay
from .config import Settings, config_path

AGENTS = ('claude', 'codex')


def which(command):
    """An absolute path to the executable (a relative PATH entry gives a relative one), or None."""
    path = shutil.which(command)
    return os.path.abspath(path) if path else None


class ACPServer(uvicorn.Server):
    async def shutdown(self, sockets=None):
        # End EventSource responses before Uvicorn waits for connections to drain.
        self.config.app.state.stopping = True
        await super().shutdown(sockets)


def setup() -> dict:
    """Create the configuration once, then connect ACP to every agent CLI that is installed."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    found = {name: which(name) for name in AGENTS}
    created = False
    try:
        with open(path, 'x', opener=lambda p, flags: os.open(p, flags, 0o600)) as file:
            provider = next((name for name in AGENTS if found[name]), 'claude')
            file.write('# ACP user configuration. Environment variables override these values.\n'
                       f'DEFAULT_PROVIDER={provider}\nDEFAULT_REPO=\n'
                       'ACP_URL=http://127.0.0.1:8765\n'
                       f'CLAUDE_COMMAND={found["claude"] or "claude"}\n'
                       f'CODEX_COMMAND={found["codex"] or "codex"}\n')
        created = True
    except FileExistsError:
        pass
    settings = Settings.from_env()
    # Detect what ACP would actually run: an existing configuration may name an agent outside PATH.
    commands = {'claude': settings.claude_command, 'codex': settings.codex_command}
    found = {name: which(command) for name, command in commands.items()}
    values = {**{k: v for k, v in dotenv_values(path).items() if v is not None}, **os.environ}
    enabled = values.get('ACP_ACTIVITY_HOOKS', '1').strip().lower() not in {'0', 'false', 'no', 'off'}
    return {'config': str(path), 'data': str(settings.data_dir), 'created': created,
            'agents': {name: {'command': found[name] or commands[name], 'found': bool(found[name])}
                       for name in AGENTS},
            'hooks': claude_activity.setup_hooks(settings.data_dir, sys.executable, found, enabled)}


@contextmanager
def service_lock(settings):
    with (settings.data_dir / 'service.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def locked(settings) -> bool:
    try:
        with service_lock(settings):
            return False
    except BlockingIOError:
        return True


def metadata(settings) -> dict:
    try:
        value = json.loads((settings.data_dir / 'service.json').read_text())
        url = urlparse(value['url'])
        if url.scheme != 'http' or url.hostname != '127.0.0.1' or not url.port or url.path:
            return {}
        if not isinstance(value['token'], str) or not isinstance(value['pid'], int):
            return {}
        return value
    except (OSError, ValueError, KeyError, TypeError):
        return {}


def control(info, method='GET', action='status'):
    with httpx.Client(timeout=2, trust_env=False) as client:
        response = client.request(method, info['url'] + '/_acp/' + action,
                                  headers={'Authorization': 'Bearer ' + info['token']})
        response.raise_for_status()
        return response.json()


def status(settings) -> dict:
    running = locked(settings)
    result = {'running': running, 'reachable': False, 'healthy': False, 'log': str(settings.data_dir / 'service.log')}
    info = metadata(settings)
    if running and info:
        result.update(pid=info['pid'], url=info['url'])
        try:
            health = control(info)
            result['reachable'] = True
            result['healthy'] = bool(health['worker_running']) and health['worker_error'] is None
            result['worker_error'] = health['worker_error']
        except (httpx.HTTPError, ValueError, KeyError):
            pass
    result['comet'] = comet_status(settings)
    return result


# Comet, the macOS overlay, rides along with the service: acp start opens it (building it first
# when needed) and acp stop quits it. It never decides whether the service itself starts or stops.
def comet_status(settings) -> dict:
    """running, log, and why Comet is not running when it should be; acp overlay status has the details."""
    if sys.platform != 'darwin':
        return {'running': False, 'skipped': 'Comet is a native macOS overlay.'}
    current = overlay.status(settings)
    result = {'running': current['running'], 'log': current['log']}
    if not current['running'] and not settings.overlay_enabled:
        result['skipped'] = 'ACP_OVERLAY=0'
    return result


def comet_start(settings, url: str) -> dict:
    current = comet_status(settings)
    if current['running'] or current.get('skipped'):
        return current
    try:
        built = overlay.build(settings)
        if built.get('skipped'):
            return {**current, 'skipped': built['skipped']}
        overlay.start(settings, url)
    except (ValueError, OSError) as exc:
        lines = str(exc).splitlines() or ['Comet did not start.']
        return {**current, 'error': lines[0] + (' Run acp overlay build for details.' if len(lines) > 1 else '')}
    return comet_status(settings)


def comet_stop(settings) -> dict:
    if sys.platform != 'darwin':
        return comet_status(settings)
    try:
        overlay.stop(settings)
    except (ValueError, OSError) as exc:
        return {**comet_status(settings), 'error': str(exc)}
    return comet_status(settings)


def start(settings, port=None) -> dict:
    current = status(settings)
    if current['reachable']:
        current['comet'] = comet_start(settings, current['url'])
        return current
    process = None
    if not current['running']:
        url = urlparse(settings.base_url)
        if url.scheme != 'http' or url.hostname != '127.0.0.1':
            raise ValueError('Background service ACP_URL must use http://127.0.0.1:<port>')
        port = port if port is not None else (url.port or 8765)
        if not 0 <= port <= 65535:
            raise ValueError('Port must be between 0 and 65535')
        env = {**os.environ, 'ACP_CONFIG_FILE': str(config_path()),
               'ACP_DATA_DIR': str(settings.data_dir)}
        log_path = settings.data_dir / 'service.log'
        # Rotate between starts to keep logs bounded without truncating a running writer.
        if log_path.exists() and log_path.stat().st_size > 10 * 1024 * 1024:
            log_path.replace(log_path.with_suffix('.log.1'))
        with open(log_path, 'ab', opener=lambda p, flags: os.open(p, flags, 0o600)) as log:
            process = subprocess.Popen([sys.executable, '-I', '-m', 'acp.daemon', str(port)],
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       cwd=settings.data_dir, env=env, start_new_session=True,
                                       close_fds=True)
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        current = status(settings)
        if current['healthy']:
            current['comet'] = comet_start(settings, current['url'])
            return current
        if process and process.poll() is not None and not current['running']:
            break
        time.sleep(.1)
    raise RuntimeError(f'ACP did not become ready. Inspect {settings.data_dir / "service.log"}')


def stop(settings) -> dict:
    comet = comet_stop(settings)
    if not locked(settings):
        return {'running': False, 'healthy': False, 'comet': comet}
    info = metadata(settings)
    if not info:
        raise RuntimeError('Service is starting or metadata is missing; retry stop shortly.')
    try:
        control(info, 'POST', 'stop')
    except (httpx.HTTPError, ValueError) as exc:
        raise RuntimeError('Service control is unavailable. Inspect service.log; no PID was signalled.') from exc
    deadline = time.monotonic() + 20
    while locked(settings):
        if time.monotonic() >= deadline:
            raise RuntimeError('Service is still shutting down. Inspect service.log and retry status.')
        time.sleep(.1)
    return {'running': False, 'healthy': False, 'comet': comet}


def server_main(port: int):
    from .api import create_app

    settings = Settings.from_env()
    os.umask(0o077)
    with service_lock(settings), socket.socket() as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('127.0.0.1', port))
        settings.base_url = f'http://127.0.0.1:{sock.getsockname()[1]}'
        token = secrets.token_urlsafe(32)
        app = create_app(settings)
        server = ACPServer(uvicorn.Config(app, log_level='info', timeout_graceful_shutdown=3))

        def authorize(request):
            if not secrets.compare_digest(request.headers.get('authorization', ''), 'Bearer ' + token):
                raise HTTPException(403, 'Invalid service control token')

        @app.get('/_acp/status', include_in_schema=False)
        def service_status(request: Request):
            authorize(request)
            return {'worker_running': bool(app.state.worker.tasks),
                    'worker_error': app.state.worker.last_error}

        @app.post('/_acp/stop', include_in_schema=False)
        def service_stop(request: Request):
            authorize(request)
            server.should_exit = True
            return {'stopping': True}

        path = settings.data_dir / 'service.json'
        temp = path.with_suffix('.tmp')
        temp.write_text(json.dumps({'url': settings.base_url, 'pid': os.getpid(), 'token': token}))
        temp.chmod(0o600)
        temp.replace(path)
        try:
            server.run(sockets=[sock])
        finally:
            path.unlink(missing_ok=True)


if __name__ == '__main__':
    server_main(int(sys.argv[1]))
