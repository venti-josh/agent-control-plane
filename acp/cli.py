from __future__ import annotations

import argparse
import json
import os
import sys
import time
import webbrowser
from collections import deque
from pathlib import Path

import httpx

from . import __version__
from .config import STATES, Settings
from .retries import retry_delay
from .command_journal import PendingCommand


def request(base: str, method: str, path: str, data: dict | None = None,
            idempotency_key: str | None = None):
    mutation = method == 'POST' and (path == '/api/tasks' or
        (path.startswith('/api/tasks/') and path.rsplit('/', 1)[-1] in
         {'comments', 'request-input', 'submit-for-review', 'request-changes'}))
    command = PendingCommand(base, method, path, data, idempotency_key) if mutation else None
    try:
        result = _request(base, method, path, data, command.key if command else None)
    except RejectedCommand:
        if command:
            command.resolve()
        raise
    except Exception as exc:
        if command:
            raise RuntimeError(f'{exc} Unresolved request ID: {command.key}. Retry the identical command; '
                               'its saved identity will be reused. Do not submit it with a new ID.') from exc
        raise
    if command:
        command.resolve()
    return result


class RejectedCommand(RuntimeError):
    pass


def _request(base, method, path, data, key):
    with httpx.Client(timeout=30, trust_env=False) as client:
        for attempt in range(3):
            # Older servers ignore request keys. Never replay a write against one.
            protected = False
            if key is not None:
                health = client.get(base.rstrip('/') + '/api/health')
                health.raise_for_status()
                protected = 'command-receipts' in health.json().get('capabilities', [])
                if not protected:
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
                    error_type = RejectedCommand if response.status_code < 500 and response.status_code not in {408, 425, 429} else RuntimeError
                    raise error_type(f'HTTP {response.status_code}: {message}')
                return response.json()
            except (httpx.TransportError, ValueError):
                if not retryable or attempt == 2:
                    raise
                time.sleep(0.5 * (2 ** attempt))
    raise RuntimeError('ACP request did not complete')


def main() -> None:
    parser = argparse.ArgumentParser(prog="acp", description="Personal Claude/Codex task orchestration")
    parser.add_argument('--version', action='version', version=f'acp {__version__}')
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
    create.add_argument("--provider", metavar="{claude,codex}")
    create.add_argument("--repo")
    create.add_argument("--state", choices=["planning", "todo"], default="planning")
    sub.add_parser("list")
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
    answer.add_argument("--blocker-id", required=True, help="The blocker_comment_id of the question you are answering")
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
    claude = sub.add_parser('claude', help='Migrate saved Claude Desktop Code sessions between accounts')
    csub = claude.add_subparsers(dest='claude_command', required=True)
    for name in ('inventory', 'migrate', 'rollback', 'status'):
        command = csub.add_parser(name)
        command.add_argument('--sessions-dir', type=Path,
                             default=Path.home() / 'Library/Application Support/Claude/claude-code-sessions')
        command.add_argument('--projects-dir', type=Path,
                             default=Path(os.getenv('CLAUDE_CONFIG_DIR') or str(Path.home() / '.claude')) / 'projects')
        command.add_argument('--backup-dir', type=Path, help='Default: $ACP_HOME/claude-migrations')
        if name == 'migrate':
            command.add_argument('--to', required=True, dest='destination', metavar='ACCOUNT/ORG')
            command.add_argument('--from', dest='source', metavar='ACCOUNT/ORG',
                                 help='Required unless exactly one other populated account/org exists')
            command.add_argument('--dry-run', action='store_true', help='Print the migration plan without any writes')
        elif name == 'rollback':
            command.add_argument('migration_id')
    activity = sub.add_parser('activity', help='Observe Codex and Claude agents across local chats using hooks')
    activity.add_argument('activity_command', choices=['install', 'status', 'uninstall'])
    activity.add_argument('--target', choices=['codex', 'claude', 'all'],
                          help='Hook set to install or remove (default codex); status shows all unless given')
    activity.add_argument('--codex-home', type=Path, default=Path(os.getenv('CODEX_HOME', str(Path.home()/'.codex'))))
    activity.add_argument('--claude-config-dir', type=Path,
                          default=Path(os.getenv('CLAUDE_CONFIG_DIR') or str(Path.home()/'.claude')).expanduser())
    activity.add_argument('--dry-run', action='store_true', help='Show the settings diff an install would write')
    update = sub.add_parser('update', help='Update this install to the latest release (or a local checkout) '
                                           'and restart the service and overlay if they were running')
    update.add_argument('--version', dest='target_version', metavar='X.Y.Z', help='Install this release instead of the latest')
    update.add_argument('--source', type=Path, help='Install from a local ACP checkout instead of a release')
    update.add_argument('--check', action='store_true', help='Only report the installed and available versions')
    update.add_argument('--reinstall', action='store_true', help='Install even if the target is not newer')
    update.add_argument('--force', action='store_true', help='Restart even though board runs would be interrupted')
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
    configure.add_argument('--provider', metavar='{codex,claude}', default='codex')
    linear_sub.add_parser('sync', help='Sync issues carrying the acp label')
    hook = linear_sub.add_parser('webhook')
    hook.add_argument('--url', required=True, help='Public HTTPS URL ending in /hooks/linear')
    imp = linear_sub.add_parser('import')
    imp.add_argument('issue_id')
    imp.add_argument('--provider', metavar='{codex,claude}')
    imp.add_argument('--repo')
    imp.add_argument('--question')
    overlay = sub.add_parser('overlay', help='macOS floating Comet companion above other applications')
    osub = overlay.add_subparsers(dest='overlay_command', required=True)
    ostart = osub.add_parser('start')
    ostart.add_argument('--url', default='', help='Local ACP origin (normally discovered automatically)')
    osub.add_parser('build', help='Compile Comet if its source changed (the installer does this on macOS)')
    for name in ['status', 'show', 'hide', 'toggle', 'reset', 'stop']:
        osub.add_parser(name)
    for command in (create, comment, answer, changes):
        command.add_argument('--request-id', help='Reuse this ID when retrying the same logical command')
    args = parser.parse_args()
    try:
        if args.command == 'claude':
            from .claude_sessions import SessionStore
            from .config import user_home
            store = SessionStore(args.sessions_dir, args.projects_dir,
                                 args.backup_dir or user_home() / 'claude-migrations')
            if args.claude_command == 'migrate':
                result = store.migrate(args.destination, args.source, args.dry_run)
            elif args.claude_command == 'rollback':
                result = store.rollback(args.migration_id)
            else:
                result = getattr(store, args.claude_command)()
            print(json.dumps(result, indent=2))
            return
        from . import daemon
        if args.command == "setup":
            print(json.dumps(daemon.setup(), indent=2))
            return
        settings = Settings.from_env()
        if args.command == 'activity':
            from . import claude_activity, codex_activity
            trackers = {'codex': (codex_activity, codex_activity.CodexActivity, args.codex_home),
                        'claude': (claude_activity, claude_activity.ClaudeActivity, args.claude_config_dir)}
            if args.activity_command == 'status' and not args.target:
                result = claude_activity.combined_summary(settings.data_dir)
            else:
                result = {}
                for name in ['codex', 'claude'] if args.target == 'all' else [args.target or 'codex']:
                    module, tracker, home = trackers[name]
                    database = settings.data_dir / f'{name}-activity.sqlite3'
                    if args.activity_command == 'install':
                        result[name] = module.install_hooks(home, database, sys.executable, dry_run=args.dry_run)
                    elif args.activity_command == 'uninstall':
                        result[name] = module.uninstall_hooks(home, database)
                    else:
                        result[name] = tracker(database).summary()
                if args.dry_run and args.activity_command == 'install':
                    print(''.join(item['diff'] for item in result.values()) or 'No changes.', end='')
                    return
                result = result[name] if len(result) == 1 else result
            print(json.dumps(result, indent=2))
            return
        if args.command == 'update':
            from .update import update
            print(json.dumps(update(settings, args.target_version, args.source, args.check,
                                    args.reinstall, args.force), indent=2))
            return
        if args.command == 'overlay':
            from . import overlay
            if args.overlay_command == 'start':
                result = overlay.start(settings, args.url)
            elif args.overlay_command in {'build', 'status', 'stop'}:
                result = getattr(overlay, args.overlay_command)(settings)
            else:
                result = overlay.control(settings, args.overlay_command)
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
                             {"body": args.body, "resume": True, "in_reply_to": args.blocker_id}, idempotency_key=args.request_id)
        elif args.command == "changes":
            result = request(base, "POST", f"/api/tasks/{args.task_id}/request-changes", {"feedback": args.feedback}, idempotency_key=args.request_id)
        elif args.command in {"cancel", "reset-session"}:
            result = request(base, "POST", f"/api/tasks/{args.task_id}/{args.command}")
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
