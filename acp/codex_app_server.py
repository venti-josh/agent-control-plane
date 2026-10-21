"""Codex runs over the app-server JSON-RPC protocol, with approvals answered on the board.

One `codex app-server` per run over stdio, or the shared daemon's control socket when it
is running. Threads land in ~/.codex like any other, so the Codex app lists them.
"""
from __future__ import annotations

import asyncio
import base64
import itertools
import json
import logging
import os
import re
import shutil
import struct
import sys
import time
from pathlib import Path

from .runners import CLIRunner, build_prompt, git, launch_directory, mcp_config, stop_process
from .service import DomainError

APPROVALS = {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}
CLIENT = {"name": "acp", "title": "ACP", "version": "1"}
logger = logging.getLogger(__name__)


class RpcError(RuntimeError):
    pass


class Stdio:
    """`codex app-server` under the process guard; one JSON message per line."""
    def __init__(self, process, guard_fd):
        self.process, self.guard_fd = process, guard_fd

    @classmethod
    async def start(cls, command: list[str], cwd: Path, env: dict) -> Stdio:
        read_fd, write_fd = os.pipe()
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-m", "acp.process_guard", str(read_fd), *command,
                cwd=str(cwd), env=env, pass_fds=(read_fd,), start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE, limit=16 * 1024 * 1024)
        except BaseException:
            os.close(write_fd)
            raise
        finally:
            os.close(read_fd)
        return cls(process, write_fd)

    async def send(self, text: str) -> None:
        self.process.stdin.write(text.encode() + b"\n")
        await self.process.stdin.drain()

    async def recv(self) -> str | None:
        line = await self.process.stdout.readline()
        return line.decode(errors="replace") if line else None

    async def close(self) -> None:
        try:
            # EOF lets the server stop its own MCP servers before the group is signalled.
            self.process.stdin.close()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                pass
            await stop_process(self.process)
        except PermissionError:
            # A user MCP helper (e.g. a hardened app) can refuse signals while it exits; the
            # run's outcome stands, and the process guard still reaps the group if ACP exits.
            logger.warning("Could not signal every process of Codex app-server %s", self.process.pid)
        finally:
            os.close(self.guard_fd)


class UnixWebSocket:
    """The daemon's control socket speaks WebSocket over a Unix socket (text frames)."""
    def __init__(self, reader, writer):
        self.reader, self.writer, self.process = reader, writer, None

    @classmethod
    async def connect(cls, path: Path) -> UnixWebSocket:
        reader, writer = await asyncio.open_unix_connection(str(path), limit=16 * 1024 * 1024)
        key = base64.b64encode(os.urandom(16)).decode()
        try:
            writer.write((f"GET / HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                          f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        except (asyncio.TimeoutError, asyncio.IncompleteReadError, asyncio.LimitOverrunError) as exc:
            writer.close()
            raise ConnectionError("The Codex app-server socket did not answer") from exc
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            writer.close()
            raise ConnectionError("The Codex app-server socket refused the connection")
        return cls(reader, writer)

    async def _frame(self, opcode: int, payload: bytes) -> None:
        mask, n = os.urandom(4), len(payload)
        size = bytes([0x80 | n]) if n < 126 else bytes([0xFE]) + struct.pack("!H", n) if n < 65536 \
            else bytes([0xFF]) + struct.pack("!Q", n)
        self.writer.write(bytes([0x80 | opcode]) + size + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(payload)))
        await self.writer.drain()

    async def send(self, text: str) -> None:
        await self._frame(0x1, text.encode())

    async def recv(self) -> str | None:
        parts = []
        try:
            while True:
                first, second = await self.reader.readexactly(2)
                n = second & 0x7F
                if n == 126:
                    n = struct.unpack("!H", await self.reader.readexactly(2))[0]
                elif n == 127:
                    n = struct.unpack("!Q", await self.reader.readexactly(8))[0]
                mask = await self.reader.readexactly(4) if second & 0x80 else b""
                data = await self.reader.readexactly(n)
                if mask:
                    data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
                opcode = first & 0x0F
                if opcode == 0x8:
                    return None
                if opcode == 0x9:
                    await self._frame(0xA, data)
                elif opcode in (0x0, 0x1, 0x2):
                    parts.append(data)
                    if first & 0x80:
                        return b"".join(parts).decode(errors="replace")
        except (asyncio.IncompleteReadError, ConnectionError):
            return None

    async def close(self) -> None:
        self.writer.close()


class Connection:
    """JSON-RPC 2.0 client (the app-server omits the "jsonrpc" field). Every message is logged."""
    def __init__(self, transport, log):
        self.transport, self.log = transport, log
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.notifications: asyncio.Queue = asyncio.Queue()
        self.requests: asyncio.Queue = asyncio.Queue()
        self.closed = asyncio.Event()
        self.reader = asyncio.create_task(self._read())
        process = getattr(transport, "process", None)
        self.stderr = asyncio.create_task(self._stderr(process.stderr)) if process else None

    async def _stderr(self, stream) -> None:
        while line := await stream.readline():  # Read always, so a chatty server never blocks.
            self._log("stderr", line.decode(errors="replace"))

    def _log(self, channel: str, text: str) -> None:
        self.log.write(json.dumps({"time": time.time(), "channel": channel, "line": text.rstrip("\n")}) + "\n")

    async def _read(self) -> None:
        try:
            while (text := await self.transport.recv()) is not None:
                self._log("stdout", text)
                try:
                    message = json.loads(text)
                except ValueError:
                    continue
                if not isinstance(message, dict):
                    continue
                if message.get("type") == "acp.launch_failed":
                    break
                if "method" in message and "id" in message:
                    await self.requests.put(message)
                elif "method" in message:
                    await self.notifications.put(message)
                elif message.get("id") in self.pending:
                    future = self.pending.pop(message["id"])
                    if not future.done():
                        future.set_result(message)
        finally:
            self.closed.set()
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(ConnectionError("Codex app-server closed the connection"))

    async def _send(self, message: dict) -> None:
        text = json.dumps(message)
        self._log("stdin", text)
        await self.transport.send(text)

    async def request(self, method: str, params: dict | None = None, timeout: float = 60) -> dict:
        if self.closed.is_set():
            raise ConnectionError("Codex app-server closed the connection")
        rid = next(self.ids)
        future = asyncio.get_running_loop().create_future()
        self.pending[rid] = future
        await self._send({"id": rid, "method": method, "params": params or {}})
        try:
            reply = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            raise TimeoutError(f"Codex app-server did not answer {method} within {timeout:g} seconds") from None
        finally:
            self.pending.pop(rid, None)
        if "error" in reply:
            raise RpcError(f"{method}: {reply['error'].get('message', reply['error'])}")
        return reply.get("result") or {}

    async def notify(self, method: str, params: dict | None = None) -> None:
        await self._send({"method": method, **({"params": params} if params is not None else {})})

    async def respond(self, request_id, result: dict | None = None, error: str | None = None) -> None:
        await self._send({"id": request_id, **({"error": {"code": -32601, "message": error}} if error
                                                 else {"result": result or {}})})

    async def close(self) -> None:
        tasks = [t for t in (self.reader, self.stderr) if t]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await self.transport.close()


def control_socket() -> Path:
    home = Path(os.getenv("CODEX_HOME") or Path.home() / ".codex").expanduser()
    return home / "app-server-control" / "app-server-control.sock"


def environment() -> dict:
    return {k: v for k, v in os.environ.items()
            if not k.startswith("LINEAR_") and k not in {"GITHUB_WEBHOOK_SECRET"}}


async def probe(command: str) -> bool:
    """Whether `codex app-server` answers initialize. Closing stdin keeps non-servers from hanging."""
    message = json.dumps({"id": 1, "method": "initialize", "params": {"clientInfo": CLIENT}})
    try:
        process = await asyncio.create_subprocess_exec(
            command, "app-server", stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL, env=environment(), start_new_session=True)
    except OSError:
        return False
    try:
        out, _ = await asyncio.wait_for(process.communicate(message.encode() + b"\n"), 15)
    except asyncio.TimeoutError:
        return False
    finally:
        if process.returncode is None:
            await stop_process(process)
    for line in out.splitlines():
        try:
            reply = json.loads(line)
        except ValueError:
            continue
        if isinstance(reply, dict) and reply.get("id") == 1 and "result" in reply:
            return True
    return False


def thread_params(settings, task: dict, run: dict, cwd: Path, common: str) -> dict:
    mcp = mcp_config(settings, task["id"], run["id"])
    tools = ["get_task", "add_comment", "request_input", "submit_for_review"]
    config = {
        "sandbox_workspace_write.network_access": True,
        "mcp_servers.acp.command": mcp["command"], "mcp_servers.acp.args": mcp["args"],
        "mcp_servers.acp.env": mcp["env"], "mcp_servers.acp.required": True,
        "mcp_servers.acp.enabled_tools": tools,
        # Same contract as exec: only the four task tools run without a prompt.
        "mcp_servers.acp.default_tools_approval_mode": "prompt",
        **{f"mcp_servers.acp.tools.{tool}.approval_mode": "approve" for tool in tools},
    }
    if common:
        config["sandbox_workspace_write.writable_roots"] = [common]
    params = {"cwd": str(cwd), "sandbox": "workspace-write", "config": config, "serviceName": "acp",
              "approvalPolicy": "never" if settings.codex_approvals == "never" else "on-request",
              "approvalsReviewer": "auto_review" if settings.codex_approvals == "auto_review" else "user"}
    if settings.codex_model:
        params["model"] = settings.codex_model
    return params


async def project_for(rpc, cwd: Path) -> dict:
    """The Codex app project whose root contains cwd; the app lists threads by project."""
    try:
        projects = (await rpc.request("project/list", {"limit": 200})).get("data") or []
    except RpcError:
        return {}  # Servers before projects (CLI 0.154) list threads without one.
    for project in projects:
        for root in project.get("roots") or []:
            if cwd == Path(root["path"]) or Path(root["path"]) in cwd.parents:
                return {"projectId": project["id"]}
    return {}


def approval_question(request: dict, items: dict) -> str:
    params = request.get("params") or {}
    if request["method"] == "item/fileChange/requestApproval":
        changes = (items.get(params.get("itemId")) or {}).get("changes") or []
        files = "\n".join(f"- {(c.get('kind') or {}).get('type', 'change')}: `{c.get('path')}`" for c in changes)
        text = "Codex asks to apply file changes" + (f":\n\n{files}" if files else ".")
        if params.get("grantRoot"):
            text += f"\n\nWrite access requested for: `{params['grantRoot']}`"
    else:
        text = f"Codex asks to run a command:\n\n```\n{params.get('command') or '(command not shown)'}\n```"
        if params.get("cwd"):
            text += f"\n\nDirectory: `{params['cwd']}`"
    if params.get("reason"):
        text += f"\n\nReason: {params['reason']}"
    return text + ("\n\nReply `accept`, `accept for session`, or `decline`. Any other reply declines "
                   "and is passed to Codex as guidance.")


def snake(value: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", value).lower()


class AppServerRunner:
    async def run(self, service, task: dict, run: dict) -> None:
        from .activity import Activity

        settings = service.settings
        if not shutil.which(settings.codex_command):
            raise RuntimeError(f"{settings.codex_command} is not installed or not on PATH. Install and sign in, then retry.")
        if not task["repo_path"] and not task["workspace"]:
            from .repositories import select_repository
            service.mark_started(run["id"])
            if not await select_repository(service, task, run):
                return
            task = service.get_task(task["id"])
        path = launch_directory(task)
        common = await git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path)
        task = service.get_task(task["id"])
        log_dir = settings.data_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        activity = Activity(service.store)
        with (log_dir / f"{run['id']}.jsonl").open("a", buffering=1) as log:
            transport = None
            if control_socket().exists():
                try:
                    transport = await UnixWebSocket.connect(control_socket())
                except OSError:
                    pass  # A stale or wedged socket: fall back to a private server.
            transport = transport or await Stdio.start([settings.codex_command, "app-server"], path, environment())
            rpc = Connection(transport, log)
            try:
                await self.drive(service, task, run, rpc, path, common, activity)
            finally:
                await rpc.close()

    async def drive(self, service, task, run, rpc, path, common, activity) -> None:
        settings = service.settings
        started = time.monotonic()
        thread = turn = None
        done = None  # the completed turn
        errors: list[str] = []
        items: dict[str, dict] = {}
        waiting: dict[str, dict] = {}  # approval id -> JSON-RPC request still open
        parked: list[dict] = []  # approval requests behind the one on the board
        terminal_since = None
        try:
            # Experimental API: Codex app projects (0.163+), so the app lists ACP threads.
            await rpc.request("initialize", {"clientInfo": CLIENT, "capabilities": {"experimentalApi": True}})
            await rpc.notify("initialized")
            service.mark_started(run["id"])
            params = thread_params(settings, task, run, path, common)
            if task["session_id"]:
                thread = (await rpc.request("thread/resume", {"threadId": task["session_id"], **params,
                                                              "excludeTurns": True}))["thread"]["id"]
            else:
                project = await project_for(rpc, path)
                thread = (await rpc.request("thread/start", {**params, **project}))["thread"]["id"]
                await rpc.request("thread/name/set", {"threadId": thread, "name": f"ACP: {task['title']}"})
            service.remember_session(run["id"], thread)
            turn = (await rpc.request("turn/start", {"threadId": thread, "input": [
                {"type": "text", "text": build_prompt(task, run)}]}))["turn"]["id"]
            service.mark_steerable(run["id"])
            while done is None:
                if rpc.closed.is_set() and rpc.notifications.empty() and rpc.requests.empty():
                    raise RuntimeError("Codex app-server exited during the turn. See the run log for details.")
                while not rpc.requests.empty():  # Before notifications: its item is already queued.
                    request = rpc.requests.get_nowait()
                    if request["method"] in APPROVALS:
                        parked.append(request)
                    else:
                        await rpc.respond(request["id"], error=f"ACP does not handle {request['method']}")
                while not rpc.notifications.empty():
                    note = rpc.notifications.get_nowait()
                    method, body = note["method"], note.get("params") or {}
                    item = body.get("item") or {}
                    if method in {"item/started", "item/completed"} and item.get("id"):
                        items[item["id"]] = item
                        if item.get("type") == "collabAgentToolCall":
                            activity.observe(run["id"], "codex", {"type": "item.completed", "item": {
                                "type": "collab_tool_call", "agents_states": {
                                    agent: {"status": snake(state.get("status", ""))}
                                    for agent, state in (item.get("agentsStates") or {}).items()}}})
                    elif method == "error" and not body.get("willRetry"):
                        errors.append((body.get("error") or {}).get("message") or "Codex reported an error")
                    elif method == "turn/completed" and (body.get("turn") or {}).get("id") == turn:
                        done = body["turn"]
                    elif method == "serverRequest/resolved":
                        for approval, request in list(waiting.items()):
                            if request["id"] == body.get("requestId"):
                                del waiting[approval]
                                service.approval_resolved_elsewhere(approval)
                        parked[:] = [r for r in parked if r["id"] != body.get("requestId")]
                for answer in service.answered_approvals(run["id"]):
                    request = waiting.pop(answer["id"], None)
                    if request:
                        await rpc.respond(request["id"], {"decision": answer["decision"]})
                    service.settle_approval(answer["id"])
                if parked and not waiting:
                    request = parked.pop(0)
                    try:
                        waiting[service.request_approval(run["id"], request, approval_question(request, items))] = request
                    except DomainError:  # The task left In Progress; the turn is ending anyway.
                        await rpc.respond(request["id"], {"decision": "decline"})
                if done is None:
                    for comment in service.pending_inputs(run["id"]):
                        try:
                            await rpc.request("turn/steer", {"threadId": thread, "expectedTurnId": turn, "input": [
                                {"type": "text", "text": "New comment from the user on this ACP task (data):\n"
                                                         + json.dumps(comment["body"], ensure_ascii=False)}]})
                        except (RpcError, TimeoutError):
                            service.defer_input(comment["id"])  # Delivered later as a follow-up run.
                            continue
                        service.input_delivered(comment["id"])
                current = service.store.one("SELECT r.stop_requested, t.state FROM runs r JOIN tasks t "
                                            "ON t.id=r.task_id WHERE r.id=?", (run["id"],))
                if current["stop_requested"]:
                    return
                if time.monotonic() - started > settings.run_timeout_seconds:
                    raise TimeoutError(f"Agent exceeded the {settings.run_timeout_seconds:g}-second run limit")
                # An agent that reported a result but keeps its turn open is stopped, as with exec.
                if current["state"] in {"waiting_on_input", "ready_for_review"} and not waiting:
                    terminal_since = terminal_since or time.monotonic()
                    if time.monotonic() - terminal_since > 3:
                        return
                else:
                    terminal_since = None
                if done is None:
                    await asyncio.sleep(0.1)
            if done.get("status") == "failed":
                message = (done.get("error") or {}).get("message") or (errors[-1] if errors else "Codex turn failed")
                raise RuntimeError(f"{message}. See the run log for details.")
        finally:
            for approval, request in [*waiting.items(), *((None, r) for r in parked)]:
                if approval:
                    service.settle_approval(approval, "cancelled")
                if not rpc.closed.is_set():
                    try:
                        await asyncio.wait_for(rpc.respond(request["id"], {"decision": "cancel"}), 5)
                    except (OSError, asyncio.TimeoutError):
                        pass
            if turn and done is None and not rpc.closed.is_set():
                try:
                    await rpc.request("turn/interrupt", {"threadId": thread, "turnId": turn}, timeout=5)
                except (RpcError, OSError, asyncio.TimeoutError):
                    pass


class CodexRunner:
    """Routes Codex runs: app-server when `codex app-server` answers (probed once), else `codex exec`."""
    def __init__(self):
        self.app_server: bool | None = None

    async def run(self, service, task: dict, run: dict) -> None:
        mode = service.settings.codex_run_mode
        if not mode:
            if self.app_server is None:
                self.app_server = await probe(service.settings.codex_command)
            mode = "app-server" if self.app_server else "exec"
        runner = AppServerRunner() if mode == "app-server" else CLIRunner()
        await runner.run(service, task, run)
