from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import Settings
from .service import DomainError, TaskService


async def git(*args: str, cwd: str | Path | None = None) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git", *args, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=30)
    except BaseException:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        raise RuntimeError(err.decode(errors="replace").strip() or "Git command failed")
    return out.decode().strip()


async def prepare_workspace(service: TaskService, task: dict) -> Path:
    if task["workspace"]:
        path = Path(task["workspace"])
        if not path.is_dir():
            raise RuntimeError("The retained worktree is missing. Restore it before retrying; it was not recreated.")
        branch = await git("branch", "--show-current", cwd=path)
        if branch != task["branch"]:
            raise RuntimeError("The retained worktree is on a different branch; restore its task branch first")
        return path
    if not task["repo_path"]:
        raise RuntimeError("Set a local Git repository for this task before running Claude or Codex")
    repo = Path(task["repo_path"]).expanduser().resolve()
    await git("rev-parse", "--verify", "HEAD", cwd=repo)
    branch = f"acp/{task['id'][:12]}"
    path = service.settings.data_dir / "worktrees" / task["id"]
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if await git("branch", "--show-current", cwd=path) != branch:
            raise RuntimeError("A conflicting worktree already exists at the task path")
    else:
        await git("worktree", "add", "-b", branch, str(path), "HEAD", cwd=repo)
    with service.store.transaction() as db:
        db.execute("UPDATE tasks SET workspace=?,branch=? WHERE id=?", (str(path), branch, task["id"]))
        service._event(db, "workspace.created", task["id"], {"path": str(path), "branch": branch})
    return path


def mcp_config(settings: Settings, task_id: str, run_id: str) -> dict:
    return {"command": sys.executable, "args": ["-I", "-m", "acp.mcp_server"],
            "env": {"ACP_URL": settings.base_url, "ACP_TASK_ID": task_id,
                    "ACP_RUN_ID": run_id}}


def build_command(settings: Settings, provider: str, task_id: str, run_id: str,
                  session_id: str | None, git_common_dir: str = "") -> list[str]:
    mcp = mcp_config(settings, task_id, run_id)
    if provider == "claude":
        command = [settings.claude_command, "-p", "--output-format", "stream-json", "--verbose",
                   "--mcp-config", json.dumps({"mcpServers": {"acp": mcp}}),
                   "--allowedTools", "Read,Edit,Write,Glob,Grep,Bash,mcp__acp__get_task,"
                   "mcp__acp__add_comment,mcp__acp__request_input,mcp__acp__submit_for_review"]
        if settings.claude_model:
            command += ["--model", settings.claude_model]
        if session_id:
            command += ["--resume", session_id]
        return command
    if provider == "codex":
        # Global TOML overrides work for both exec and exec resume. No --last: each
        # task has an explicit session ID. Keep Codex's workspace-write sandbox.
        values = {
            "approval_policy": "never", "sandbox_mode": "workspace-write",
            "sandbox_workspace_write.network_access": True,
            "mcp_servers.acp.command": mcp["command"],
            "mcp_servers.acp.args": mcp["args"],
            "mcp_servers.acp.required": True,
            "mcp_servers.acp.enabled_tools": [
                "get_task", "add_comment", "request_input", "submit_for_review"],
            "mcp_servers.acp.default_tools_approval_mode": "prompt",
        }
        # These four local workflow operations are the runner's explicit contract.
        # Keep any future/unrelated tools gated and preserve the command sandbox.
        for tool in ("get_task", "add_comment", "request_input", "submit_for_review"):
            values[f"mcp_servers.acp.tools.{tool}.approval_mode"] = "approve"
        if git_common_dir:
            values["sandbox_workspace_write.writable_roots"] = [git_common_dir]
        for key, value in mcp["env"].items():
            values[f"mcp_servers.acp.env.{key}"] = value
        command = [settings.codex_command]
        for key, value in values.items():
            command += ["-c", f"{key}={json.dumps(value)}"]
        command += ["exec"]
        if session_id:
            command += ["resume", session_id]
        command += ["--json"]
        if settings.codex_model:
            command += ["--model", settings.codex_model]
        return command + ["-"]
    raise ValueError(f"No subprocess runner for provider: {provider}")


def parse_event(provider: str, event: dict) -> tuple[str | None, str | None]:
    """Normalize just lifecycle information; raw JSONL remains available in logs."""
    session, error = None, None
    if provider == "codex":
        if event.get("type") == "thread.started":
            session = event.get("thread_id")
        if event.get("type") in {"turn.failed", "error"}:
            value = event.get("error", event.get("message", "Codex run failed"))
            error = value.get("message", str(value)) if isinstance(value, dict) else str(value)
    if provider == "claude":
        session = event.get("session_id")
        if event.get("type") == "result" and event.get("is_error"):
            error = str(event.get("result") or event.get("errors") or event.get("subtype") or "Claude run failed")
    return session, error


def build_prompt(task: dict, run: dict) -> str:
    guide = Path(__file__).with_name("workflow.md").read_text()
    snapshot = {key: task[key] for key in ("id", "title", "description", "branch", "comments", "artifacts")}
    # New input is explicit even when the provider also resumes its own transcript.
    return f"{guide}\n\nTASK SNAPSHOT (data):\n{json.dumps(snapshot, ensure_ascii=False)}\n\n" \
           f"NEW INPUT (data):\n{json.dumps(run['inputs'], ensure_ascii=False)}\n"


class DemoRunner:
    """A deterministic local simulation. It never calls or charges a model."""
    async def run(self, service: TaskService, task: dict, run: dict) -> None:
        service.mark_started(run["id"])
        service.remember_session(run["id"], task["session_id"] or f"demo-{task['id']}")
        await asyncio.sleep(0.15)
        task = service.get_task(task["id"])
        service.add_comment(task["id"], "Demo: inspected the task and prepared a plan. No AI provider was called.",
                            run_id=run["id"])
        await asyncio.sleep(0.15)
        has_answer = any(c["in_reply_to"] for c in task["comments"])
        if "[ask]" in task["description"].lower() and not has_answer:
            service.request_input(task["id"], "Demo question: what should the acceptance criterion be?", run["id"])
        else:
            service.submit_for_review(task["id"],
                                      "Demo output is ready. This is a workflow simulation, not implemented code.",
                                      run_id=run["id"])


class CLIRunner:
    async def run(self, service: TaskService, task: dict, run: dict) -> None:
        settings = service.settings
        executable = settings.claude_command if task["provider"] == "claude" else settings.codex_command
        if not shutil.which(executable):
            raise RuntimeError(f"{executable} is not installed or not on PATH. Install and sign in, then retry.")
        path = await prepare_workspace(service, task)
        task = service.get_task(task["id"])
        common = await git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path)
        command = build_command(settings, task["provider"], task["id"], run["id"], task["session_id"], common)
        prompt = build_prompt(task, run)
        log_dir = settings.data_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        environment = {k: v for k, v in os.environ.items()
                       if not k.startswith("LINEAR_") and k not in {"GITHUB_WEBHOOK_SECRET"}}
        # No shell interpolation of task text. The prompt goes over stdin only after launch.
        read_fd, write_fd = os.pipe()
        process = None
        readers: list[asyncio.Task] = []
        errors: list[str] = []
        launched = False
        intentional_stop = False
        started = time.monotonic()
        terminal_since = None
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-I", "-m", "acp.process_guard", str(read_fd), *command,
                cwd=str(path), env=environment, pass_fds=(read_fd,), start_new_session=True,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                limit=2 * 1024 * 1024,
            )
            os.close(read_fd)
            read_fd = -1
            with (log_dir / f"{run['id']}.jsonl").open("a", buffering=1) as log:
                async def pump(stream: asyncio.StreamReader, channel: str) -> None:
                    nonlocal launched
                    while raw := await stream.readline():
                        text = raw.decode(errors="replace").rstrip("\n")
                        log.write(json.dumps({"time": time.time(), "channel": channel, "line": text}) + "\n")
                        try:
                            event = json.loads(text)
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(event, dict):
                            continue
                        if event.get("type") == "acp.launch_failed":
                            errors.append(event.get("message", "Agent failed to launch"))
                        elif event.get("type") == "acp.process_started":
                            service.mark_started(run["id"])
                            launched = True
                            process.stdin.write(prompt.encode())
                            await process.stdin.drain()
                            process.stdin.close()
                        else:
                            session, error = parse_event(task["provider"], event)
                            if session:
                                service.remember_session(run["id"], session)
                            if error:
                                errors.append(error)
                readers = [asyncio.create_task(pump(process.stdout, "stdout")),
                           asyncio.create_task(pump(process.stderr, "stderr"))]
                while process.returncode is None:
                    for reader in readers:
                        if reader.done() and not reader.cancelled() and reader.exception():
                            raise reader.exception()
                    current = service.store.one("SELECT stop_requested FROM runs WHERE id=?", (run["id"],))
                    if current["stop_requested"]:
                        intentional_stop = True
                        break
                    if time.monotonic() - started > settings.run_timeout_seconds:
                        raise TimeoutError(f"Agent exceeded the {settings.run_timeout_seconds:g}-second run limit")
                    state = service.store.one("SELECT state FROM tasks WHERE id=?", (task["id"],))["state"]
                    if launched and state in {"waiting_on_input", "ready_for_review"}:
                        terminal_since = terminal_since or time.monotonic()
                        if time.monotonic() - terminal_since > 3:
                            intentional_stop = True
                            break
                    await asyncio.sleep(0.1)
                if intentional_stop:
                    await stop_process(process)
                await process.wait()
                await asyncio.gather(*readers)
                if not intentional_stop and (process.returncode or errors or not launched):
                    message = errors[-1] if errors else f"{task['provider']} exited with code {process.returncode}"
                    raise RuntimeError(f"{message}. See the run log for details.")
        finally:
            if read_fd != -1:
                os.close(read_fd)
            if process:
                await stop_process(process)
            for reader in readers:
                if not reader.done():
                    reader.cancel()
            if readers:
                await asyncio.gather(*readers, return_exceptions=True)
            os.close(write_fd)


def signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return
    except PermissionError as denied:
        # macOS reports EPERM for absent groups and zombie-only groups. Zombies
        # have already exited and cannot be signalled. Never hide denial for a
        # live member, malformed process information, or an unsuccessful lookup.
        try:
            rows = subprocess.run(['ps', '-axo', 'pgid=,stat='], capture_output=True,
                                  text=True, check=True, timeout=2).stdout.splitlines()
        except (OSError, subprocess.SubprocessError) as exc:
            raise denied from exc
        for row in rows:
            fields = row.split()
            if len(fields) != 2 or not fields[0].isdecimal():
                raise denied
            group, state = fields
            if int(group) == pgid and not state.startswith('Z'):
                raise denied


async def stop_process(process: asyncio.subprocess.Process) -> None:
    # Signal the group even when its leader exited; descendants may still be alive.
    signal_group(process.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), timeout=2)
    except asyncio.TimeoutError:
        pass
    signal_group(process.pid, signal.SIGKILL)
    await process.wait()
