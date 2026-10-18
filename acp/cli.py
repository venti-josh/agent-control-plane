from __future__ import annotations

import argparse
import json
import os
import sys
import time
from uuid import uuid4
import webbrowser
from collections import deque
from pathlib import Path

import httpx

from .config import STATES, Settings
from .retries import retry_delay


def request(base: str, method: str, path: str, data: dict | None = None,
            idempotency_key: str | None = None):
    mutation = method == 'POST' and (path == '/api/tasks' or
        (path.startswith('/api/tasks/') and path.rsplit('/', 1)[-1] in
         {'comments', 'request-input', 'submit-for-review', 'request-changes'}) or
        (path.startswith('/api/desktop/runs/') and path.endswith('/action') and
         (data or {}).get('action') in {'comment', 'input', 'review'}))
    key = idempotency_key or (str(uuid4()) if mutation else None)
    with httpx.Client(timeout=30, trust_env=False) as client:
        for attempt in range(3):
            # Older servers ignore request keys. Never replay a write against one.
            protected = False
            if mutation:
                health = client.get(base.rstrip('/') + '/api/health')
                health.raise_for_status()
                protected = 'command-receipts' in health.json().get('capabilities', [])
                if (idempotency_key or attempt > 0) and not protected:
                    raise RuntimeError('This ACP server does not support idempotent commands; upgrade it first.')
            retryable = method == 'GET' or protected
            try:
                response = client.request(method, base.rstrip('/') + path, json=data,
                                          headers={'Idempotency-Key': key} if key and protected else {})
                if response.is_error and retryable and attempt < 2:
                    error = httpx.HTTPStatusError('ACP request failed', request=response.request, response=response)
                    delay = retry_delay(error, attempt)
                    # Long Retry-After windows belong to the durable queue, not a blocked CLI.
                    if delay is not None and delay <= 5:
                        time.sleep(delay)
                        continue
                if response.is_error:
                    try:
                        message = response.json().get('detail', response.text)
                    except ValueError:
                        message = response.text
                    raise RuntimeError(f'HTTP {response.status_code}: {message}')
                return response.json()
            except (httpx.TransportError, ValueError):
                if not retryable or attempt == 2:
                    raise
                time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError('ACP request did not complete')


def main() -> None:
    parser = argparse.ArgumentParser(prog="acp", description="Personal Claude/Codex task orchestration")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ["serve", "hooks"]:
        server = sub.add_parser(name)
        server.add_argument("--port", type=int, default=8765 if name == "serve" else 8766)
    for name in ["setup", "stop", "status", "open"]:
        sub.add_parser(name)
    start = sub.add_parser("start")
    start.add_argument("--port", type=int)
    logs = sub.add_parser("logs")
    logs.add_argument("--lines", type=int, default=100)
    create = sub.add_parser("create")
    create.add_argument("title")
    create.add_argument("--description", default="")
    create.add_argument("--provider", choices=["claude", "codex", "demo"])
    create.add_argument("--repo")
    create.add_argument("--state", choices=["planning", "todo"], default="planning")
    sub.add_parser("list")
    sub.add_parser("demo")
    show = sub.add_parser("show")
    show.add_argument("task_id")
    move = sub.add_parser("move")
    move.add_argument("task_id")
    move.add_argument("state", choices=STATES)
    move.add_argument("--comment")
    comment = sub.add_parser("comment")
    comment.add_argument("task_id")
    comment.add_argument("body")
    answer = sub.add_parser("answer")
    answer.add_argument("task_id")
    answer.add_argument("body")
    changes = sub.add_parser("changes")
    changes.add_argument("task_id")
    changes.add_argument("feedback")
    for name in ["cancel", "reset-session"]:
        p = sub.add_parser(name)
        p.add_argument("task_id")
    events = sub.add_parser("events")
    events.add_argument("--after", type=int, default=0)
    events.add_argument("--follow", action="store_true")
    sub.add_parser("mcp")
    activity = sub.add_parser('activity', help='Observe Codex agents across local chats using trusted hooks')
    activity.add_argument('activity_command', choices=['install', 'status', 'uninstall'])
    activity.add_argument('--codex-home', type=Path, default=Path(os.getenv('CODEX_HOME', str(Path.home()/'.codex'))))
    linear = sub.add_parser('linear', help='Configure Linear from an agent or terminal; JSON output, no secrets')
    linear_sub = linear.add_subparsers(dest='linear_command', required=True)
    linear_sub.add_parser('status')
    discover = linear_sub.add_parser('discover')
    discover.add_argument('--key-file', help='Private API key file; otherwise use saved configuration')
    configure = linear_sub.add_parser('configure')
    configure.add_argument('--key-file')
    configure.add_argument('--team-id', required=True)
    configure.add_argument('--states-file', help='Optional JSON object of lane names to status IDs')
    configure.add_argument('--create-missing', action='store_true', help='Create missing statuses in Linear')
    configure.add_argument('--provider', choices=['codex', 'claude', 'demo'], default='codex')
    linear_sub.add_parser('sync', help='Sync issues carrying the acp label')
    hook = linear_sub.add_parser('webhook')
    hook.add_argument('--url', required=True, help='Public HTTPS URL ending in /hooks/linear')
    imp = linear_sub.add_parser('import')
    imp.add_argument('issue_id')
    imp.add_argument('--provider', choices=['codex', 'claude', 'demo'])
    imp.add_argument('--repo')
    imp.add_argument('--question')
    overlay = sub.add_parser('overlay', help='macOS floating Comet companion above other applications')
    osub = overlay.add_subparsers(dest='overlay_command', required=True)
    ostart = osub.add_parser('start')
    ostart.add_argument('--url', default='', help='Local ACP origin (normally discovered automatically)')
    for name in ['status', 'show', 'hide', 'toggle', 'reset', 'stop']:
        osub.add_parser(name)
    desktop = sub.add_parser('desktop', help='Connect and operate the supported Codex app dispatcher')
    desktop.add_argument('--url', help='ACP local service URL')
    ds = desktop.add_subparsers(dest='desktop_command', required=True)
    ds.add_parser('status')
    ds.add_parser('instructions')
    ds.add_parser('disconnect')
    connect = ds.add_parser('connect')
    connect.add_argument('--thread', required=True)
    for name in ('claim', 'prepare', 'bind', 'started', 'complete', 'acknowledge', 'action',
                 'steering', 'steered', 'recover'):
        item = ds.add_parser(name)
        item.add_argument('run_id')
        if name in ('bind', 'started', 'complete', 'acknowledge', 'action', 'steered'):
            item.add_argument('--thread', default=None)
        if name == 'steered':
            item.add_argument('--comment-id', required=True)
        if name == 'prepare':
            item.add_argument('--repo', default='')
        if name in {'complete', 'acknowledge'}:
            item.add_argument('--error', default='')
        if name == 'action':
            item.add_argument('action', choices=['get', 'comment', 'input', 'review', 'activity'])
            item.add_argument('--text', default='')
            item.add_argument('--output-url', default='')
            item.add_argument('--kind', choices=['text', 'file', 'url', 'pr'], default='text')
    for command in (create, comment, answer, changes):
        command.add_argument('--request-id', help='Reuse this ID when retrying the same logical command')
    ds.choices['action'].add_argument('--request-id', help='Reuse for retries of this task action')
    args = parser.parse_args()
    try:
        from . import daemon
        if args.command == "setup":
            print(json.dumps(daemon.setup(), indent=2))
            return
        settings = Settings.from_env()
        if args.command == 'activity':
            from .codex_activity import CodexActivity, install_hooks, uninstall_hooks
            database = settings.data_dir / 'codex-activity.sqlite3'
            if args.activity_command == 'install':
                result = install_hooks(args.codex_home, database, sys.executable)
            elif args.activity_command == 'uninstall':
                result = uninstall_hooks(args.codex_home, database)
            else:
                result = CodexActivity(database).summary()
            print(json.dumps(result, indent=2))
            return
        if args.command == 'overlay':
            from . import overlay
            if args.overlay_command == 'start':
                result = overlay.start(settings, args.url)
            elif args.overlay_command in {'status', 'stop'}:
                result = getattr(overlay, args.overlay_command)(settings)
            else:
                result = overlay.control(settings, args.overlay_command)
            print(json.dumps(result, indent=2))
            return
        if args.command == 'desktop':
            base = args.url or daemon.status(settings).get('url') or settings.base_url
            action = args.desktop_command
            if action in {'status', 'instructions'}:
                result = request(base, 'GET', '/api/desktop')
                print(result['instructions'] if action == 'instructions' else json.dumps(result, indent=2))
                return
            if action in {'connect', 'disconnect'}:
                result = request(base, 'POST', '/api/desktop/connection',
                                 {'thread_id': args.thread if action == 'connect' else None})
            else:
                payload = {}
                if action in {'bind', 'started', 'action', 'complete', 'acknowledge', 'steered'}:
                    payload['thread_id'] = args.thread or (os.getenv('CODEX_THREAD_ID', '')
                                                          if action in {'started', 'action'} else '')
                if action == 'steered':
                    payload['comment_id'] = args.comment_id
                if action == 'prepare':
                    payload['repo'] = args.repo
                if action in {'complete', 'acknowledge'}:
                    payload['error'] = args.error
                if action == 'action':
                    payload.update(action=args.action, text=args.text, output_url=args.output_url, kind=args.kind)
                result = request(base, 'POST', f'/api/desktop/runs/{args.run_id}/{action}', payload,
                                 idempotency_key=getattr(args, 'request_id', None))
            print(json.dumps(result, indent=2))
            return
        if args.command in {"start", "stop", "status", "open", "logs"}:
            if args.command == "start":
                result = daemon.start(settings, args.port)
            elif args.command == "stop":
                result = daemon.stop(settings)
            elif args.command == "status":
                result = daemon.status(settings)
            elif args.command == "open":
                result = daemon.status(settings)
                if not result["reachable"]:
                    raise RuntimeError("ACP is not ready. Run acp start first.")
                if not webbrowser.open(result["url"]):
                    print("Open " + result["url"] + " in your browser.", file=sys.stderr)
            else:
                if args.lines < 1:
                    raise ValueError("--lines must be positive")
                path = settings.data_dir / "service.log"
                if path.exists():
                    with path.open(errors="replace") as log:
                        print("".join(deque(log, maxlen=args.lines)), end="")
                return
            print(json.dumps(result, indent=2))
            return
        if args.command == "serve":
            daemon.server_main(args.port)
            return
        if args.command == "hooks":
            import uvicorn
            from .api import create_app
            app = create_app(settings, hooks_only=args.command == "hooks")
            daemon.ACPServer(uvicorn.Config(app, host="127.0.0.1", port=args.port,
                                            log_level="info", timeout_graceful_shutdown=3)).run()
            return
        if args.command == "mcp":
            from .mcp_server import mcp
            mcp.run(transport="stdio")
            return
        running = daemon.status(settings)
        if not running["reachable"]:
            raise RuntimeError("The selected ACP service is unavailable. Run acp start (or acp serve); inspect acp logs.")
        base = running["url"]
        result = None
        if args.command == 'linear':
            action = args.linear_command
            data = {}
            if action in {'discover', 'configure'}:
                key_file = getattr(args, 'key_file', None)
                data['api_key'] = Path(key_file).expanduser().read_text().strip() if key_file else settings.linear_api_key
            if action == 'configure':
                data.update(team_id=args.team_id, create_missing=args.create_missing, provider=args.provider)
                if args.states_file:
                    data['states'] = json.loads(Path(args.states_file).read_text())
            elif action == 'webhook':
                data['url'] = args.url
            elif action == 'import':
                data = {'issue_id': args.issue_id, 'provider': args.provider,
                        'repo_path': args.repo, 'question': args.question}
            result = request(base, 'GET' if action == 'status' else 'POST',
                             '/api/integrations/linear/' + ('reconcile' if action == 'sync' else action),
                             None if action == 'status' else data)
        elif args.command == "create":
            result = request(base, "POST", "/api/tasks", {"title": args.title, "description": args.description,
                                                         "provider": args.provider, "repo_path": args.repo,
                                                         "state": args.state}, idempotency_key=args.request_id)
        elif args.command == "list":
            result = request(base, "GET", "/api/tasks")
        elif args.command == "show":
            result = request(base, "GET", f"/api/tasks/{args.task_id}")
        elif args.command == "move":
            result = request(base, "POST", f"/api/tasks/{args.task_id}/transition",
                             {"state": args.state, "comment": args.comment})
        elif args.command == "comment":
            result = request(base, "POST", f"/api/tasks/{args.task_id}/comments", {"body": args.body}, idempotency_key=args.request_id)
        elif args.command == "answer":
            result = request(base, "POST", f"/api/tasks/{args.task_id}/comments",
                             {"body": args.body, "resume": True}, idempotency_key=args.request_id)
        elif args.command == "changes":
            result = request(base, "POST", f"/api/tasks/{args.task_id}/request-changes", {"feedback": args.feedback}, idempotency_key=args.request_id)
        elif args.command in {"cancel", "reset-session"}:
            result = request(base, "POST", f"/api/tasks/{args.task_id}/{args.command}")
        elif args.command == "demo":
            result = [request(base, "POST", "/api/tasks", item) for item in [
                {"title": "Try the input loop", "description": "[ask] Simulate an agent asking for a requirement.",
                 "provider": "demo", "state": "todo"},
                {"title": "Try the review loop", "description": "Simulate output, then request a revision.",
                 "provider": "demo", "state": "todo"},
                {"title": "Your first real task", "description": "Choose Claude or Codex and a local Git repo, then move to Todo.",
                 "provider": "demo"},
            ]]
        elif args.command == "events":
            if args.follow:
                with httpx.stream("GET", base + f"/api/events/stream?after={args.after}",
                                  timeout=None, trust_env=False) as response:
                    response.raise_for_status()
                    for line in response.iter_lines():
                        if line.startswith("data: "):
                            print(line[6:], flush=True)
                return
            result = request(base, "GET", f"/api/events?after={args.after}")
        print(json.dumps(result, indent=2))
    except (RuntimeError, ValueError, OSError, httpx.HTTPError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
