from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from collections import deque
from pathlib import Path

import httpx

from .config import STATES, Settings


def request(base: str, method: str, path: str, data: dict | None = None):
    with httpx.Client(timeout=30, trust_env=False) as client:
        response = client.request(method, base.rstrip("/") + path, json=data)
        if response.is_error:
            try:
                message = response.json().get("detail", response.text)
            except ValueError:
                message = response.text
            raise RuntimeError(f"HTTP {response.status_code}: {message}")
        return response.json()


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
    args = parser.parse_args()
    try:
        from . import daemon
        if args.command == "setup":
            print(json.dumps(daemon.setup(), indent=2))
            return
        settings = Settings.from_env()
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
                                                         "state": args.state})
        elif args.command == "list":
            result = request(base, "GET", "/api/tasks")
        elif args.command == "show":
            result = request(base, "GET", f"/api/tasks/{args.task_id}")
        elif args.command == "move":
            result = request(base, "POST", f"/api/tasks/{args.task_id}/transition",
                             {"state": args.state, "comment": args.comment})
        elif args.command == "comment":
            result = request(base, "POST", f"/api/tasks/{args.task_id}/comments", {"body": args.body})
        elif args.command == "answer":
            task = request(base, "GET", f"/api/tasks/{args.task_id}")
            result = request(base, "POST", f"/api/tasks/{args.task_id}/comments",
                             {"body": args.body, "in_reply_to": task["blocker_comment_id"], "resume": True})
        elif args.command == "changes":
            result = request(base, "POST", f"/api/tasks/{args.task_id}/request-changes", {"feedback": args.feedback})
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
