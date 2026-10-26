from __future__ import annotations

import asyncio
import json
import os
import re
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


def launch_directory(task: dict) -> Path:
    """Choose an initial CLI directory; workspace creation belongs to the agent.

    Preserve the launch location of older sessions without managing their Git state.
    """
    value = task.get("workspace") or task.get("repo_path")
    if not value:
        raise RuntimeError("Select a local repository before launching the agent")
    path = Path(value).expanduser().resolve()
    if not path.is_dir():
        raise RuntimeError("The saved working directory is missing. Restore it before retrying.")
    return path


CLAUDE_TOOLS = ("Read,Edit,Write,Glob,Grep,Bash,mcp__acp__get_task,"
                "mcp__acp__add_comment,mcp__acp__request_input,mcp__acp__submit_for_review")
TERMINAL = {"waiting_on_input", "ready_for_review"}
SETTLE_SECONDS = 3  # After the agent reports, let its final tool calls land before ending the run.
STARTUP_GRACE_SECONDS = 30  # A Claude background session may look idle or absent this long after it starts.
STARTUP_LIMIT_SECONDS = 120  # A session listed as working without a pid gets this long after launch to start.


def agent_environment(*hide: str) -> dict:
    """The agent's environment without ACP's integration secrets (and any extra names)."""
    hidden = {"GITHUB_WEBHOOK_SECRET", *hide}
    return {k: v for k, v in os.environ.items() if not k.startswith("LINEAR_") and k not in hidden}


def mcp_config(settings: Settings, task_id: str, run_id: str = "") -> dict:
    env = {"ACP_URL": settings.base_url, "ACP_TASK_ID": task_id}
    if run_id:
        env["ACP_RUN_ID"] = run_id  # A background session keeps one config; it looks the run up.
    return {"command": sys.executable, "args": ["-I", "-m", "acp.mcp_server"], "env": env}


def build_command(settings: Settings, provider: str, task_id: str, run_id: str,
                  session_id: str | None, git_common_dir: str = "") -> list[str]:
    mcp = mcp_config(settings, task_id, run_id)
    if provider == "claude":
        command = [settings.claude_command, "-p", "--output-format", "stream-json", "--verbose",
                   "--mcp-config", json.dumps({"mcpServers": {"acp": mcp}}),
                   "--allowedTools", CLAUDE_TOOLS]
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


def background_args(settings: Settings, task: dict, saved_session: bool) -> list[str]:
    """Arguments for `claude --bg`. The prompt goes over stdin: the tool lists are variadic.

    A background session keeps its options (name, MCP config, tools, model). Resuming
    it with any other flag, or by its short id, starts a copy under a new id.
    """
    args = ["--bg"]
    if task["session_id"]:
        args += ["--resume", task["session_id"]]
        if saved_session:
            return args
    args += ["--name", task["title"][:60],
             "--mcp-config", json.dumps({"mcpServers": {"acp": mcp_config(settings, task["id"])}}),
             "--allowedTools", CLAUDE_TOOLS]
    if settings.claude_model:
        args += ["--model", settings.claude_model]
    return args


async def claude(settings: Settings, *args: str, stdin: bytes | None = None, cwd: str | Path | None = None,
                 env: dict | None = None, timeout: float = 60) -> tuple[int, str, str]:
    """Run one claude command to completion. Background sessions outlive this call."""
    proc = await asyncio.create_subprocess_exec(
        settings.claude_command, *args, cwd=cwd, env=env,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(stdin), timeout=timeout)
    except BaseException as exc:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()
        if isinstance(exc, asyncio.TimeoutError):
            raise TimeoutError(f"claude {args[0]} did not finish within {timeout:g} seconds") from None
        raise
    return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def last_line(text: str) -> str:
    lines = text.strip().splitlines()
    return lines[-1].strip() if lines else ""


async def background_supported(settings: Settings) -> bool:
    try:
        code, out, _ = await claude(settings, "--help", timeout=30)
    except OSError:  # Includes TimeoutError.
        return False
    return code == 0 and re.search(r"--bg\b", out) is not None


async def background_sessions(settings: Settings) -> dict[str, dict]:
    """Claude's background sessions by short id; stopped ones are listed without a pid."""
    code, out, err = await claude(settings, "agents", "--json", "--all")
    if code:
        raise RuntimeError(f"claude agents failed: {last_line(err) or last_line(out)}")
    return {entry["id"]: entry for entry in json.loads(out) if isinstance(entry, dict) and entry.get("id")}


def job_detail(short: str) -> str:
    """Claude's own reason a background session failed, such as "exit 1 before init", if it kept one."""
    from .claude_activity import config_home
    try:
        job = json.loads((config_home() / "jobs" / short / "state.json").read_text())
    except (OSError, ValueError):
        return ""
    # Outside a failure the same field holds the prompt or a summary of the turn.
    detail = job.get("detail") if isinstance(job, dict) and job.get("state") == "failed" else None
    return detail.strip() if isinstance(detail, str) else ""


async def stop_session(settings: Settings, short: str, note) -> bool:
    """`claude stop`, then confirm the registry shows no pid. True means the session is still live."""
    for _ in range(2):
        try:
            code, out, err = await claude(settings, "stop", short, timeout=30)
            note("stop", last_line(out) or last_line(err))
            entry = (await background_sessions(settings)).get(short)
        except (OSError, ValueError, RuntimeError) as exc:
            note("stop", f"claude stop failed: {exc}")
        else:
            if not (entry and entry.get("pid")):
                return False
        await asyncio.sleep(1)
    return True


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


def build_prompt(task: dict, run: dict, connector: str | None = None) -> str:
    guide = connector if connector is not None else Path(__file__).with_name("workflow.md").read_text()
    snapshot = {key: task[key] for key in ("id", "title", "description", "repo_path", "workspace", "comments", "artifacts")}
    snapshot["linear_url"] = task.get("linear_url")
    guide += (
        '\n\nWhen creating a branch for a task linked to Linear, include the issue identifier '
        'from the task snapshot\'s linear_url (for example, ENG-42) in the branch name so '
        'Linear can link the pull request. Keep the prefix and naming conventions required '
        'by the repository or user (for example, codex/eng-42-short-description). '
        'If no Linear identifier is available, use your normal branch naming; do not invent one. '
        'When continuing an existing branch or PR, keep it rather than renaming it solely for this convention.'
    )
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
    def __init__(self) -> None:
        self.background: bool | None = None  # probed once per service start

    async def claude_mode(self, settings: Settings) -> str:
        if settings.claude_run_mode:
            return settings.claude_run_mode
        if self.background is None:
            self.background = await background_supported(settings)
        return "background" if self.background else "headless"

    async def run(self, service: TaskService, task: dict, run: dict) -> None:
        from .activity import Activity

        activity = Activity(service.store)
        settings = service.settings
        executable = settings.claude_command if task["provider"] == "claude" else settings.codex_command
        if not shutil.which(executable):
            raise RuntimeError(f"{executable} is not installed or not on PATH. Install and sign in, then retry.")
        discovering = not task['repo_path'] and not task['workspace']
        if discovering:
            from .repositories import select_repository
            service.mark_started(run['id'])
            if not await select_repository(service, task, run):
                return
            task = service.get_task(task['id'])
        path = launch_directory(task)
        task = service.get_task(task["id"])
        if task["provider"] == "claude" and await self.claude_mode(settings) == "background":
            await self.run_background(service, task, run, path)
            return
        common = await git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=path)
        command = build_command(settings, task["provider"], task["id"], run["id"], task["session_id"], common)
        prompt = build_prompt(task, run)
        log_dir = settings.data_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        environment = agent_environment()
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
                            if not discovering:
                                service.mark_started(run["id"])
                            launched = True
                            process.stdin.write(prompt.encode())
                            await process.stdin.drain()
                            process.stdin.close()
                        else:
                            if channel == 'stdout':
                                activity.observe(run['id'], task['provider'], event)
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
                    if launched and state in TERMINAL:
                        terminal_since = terminal_since or time.monotonic()
                        if time.monotonic() - terminal_since > SETTLE_SECONDS:
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

    async def run_background(self, service: TaskService, task: dict, run: dict, path: Path) -> None:
        """Run the task as a `claude --bg` session: visible in `claude agents`, not in the Claude app.

        Claude owns the process. ACP launches or resumes the session, watches `claude agents`
        and the task state, and stops the session with `claude stop` when the run ends.
        """
        settings = service.settings
        log_dir = settings.data_dir / "logs"
        log_dir.mkdir(exist_ok=True)
        short = None
        with (log_dir / f"{run['id']}.jsonl").open("a", buffering=1) as log:
            def note(channel: str, text: str) -> None:
                log.write(json.dumps({"time": time.time(), "channel": channel, "line": text}) + "\n")

            try:
                saved = None
                if task["session_id"]:
                    saved = next((s for s in (await background_sessions(settings)).values()
                                  if s.get("sessionId") == task["session_id"]), None)
                if saved and saved.get("pid") and await stop_session(settings, saved["id"], note):
                    # Resuming a live session would copy it.
                    raise RuntimeError(f"Claude session {saved['id']} is still running; "
                                       f"stop it with `claude stop {saved['id']}` and retry")
                # The prompt is delivered at launch, so the task must already be in progress.
                service.mark_started(run["id"])
                started = time.monotonic()
                deadline = started + settings.run_timeout_seconds
                # Markers inherited from a parent Claude session would turn transcript saving off.
                code, out, err = await claude(settings, *background_args(settings, task, saved is not None),
                                              stdin=build_prompt(task, run).encode(), cwd=path,
                                              env=agent_environment("CLAUDECODE", "CLAUDE_CODE_CHILD_SESSION"),
                                              timeout=max(1.0, min(120.0, deadline - time.monotonic())))
                for line in out.splitlines() + err.splitlines():
                    note("launch", line)
                match = re.search(r"backgrounded\W+([0-9a-f]{4,})", out)
                short = match.group(1) if match else None  # Stop whatever was spawned, even on failure.
                if code or not short:
                    detail = last_line(err) or last_line(out) or "claude --bg did not report a session id"
                    raise RuntimeError(f"{detail}. See the run log for details.")
                for _ in range(50):  # The registry lists the session shortly after launch.
                    entry = (await background_sessions(settings)).get(short)
                    if entry and entry.get("sessionId"):
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"Agent exceeded the {settings.run_timeout_seconds:g}-second run limit")
                    await asyncio.sleep(0.2)
                else:
                    raise RuntimeError(f"claude agents does not list background session {short}")
                session = entry["sessionId"]
                service.remember_session(run["id"], session)
                note("agents", json.dumps(entry))
                terminal_since = None
                seen_busy = False
                status = None
                failures = misses = 0
                next_poll = 0.0
                grace_from = started  # Restarts until the session is first seen started.
                was_starting = False
                while True:
                    if service.store.one("SELECT stop_requested FROM runs WHERE id=?", (run["id"],))["stop_requested"]:
                        break
                    if time.monotonic() > deadline:
                        raise TimeoutError(f"Agent exceeded the {settings.run_timeout_seconds:g}-second run limit")
                    state = service.store.one("SELECT state FROM tasks WHERE id=?", (task["id"],))["state"]
                    if state in TERMINAL:
                        terminal_since = terminal_since or time.monotonic()
                        if time.monotonic() - terminal_since > SETTLE_SECONDS:
                            break
                    elif time.monotonic() >= next_poll:
                        next_poll = time.monotonic() + settings.poll_seconds * 4
                        try:
                            entry = (await background_sessions(settings)).get(short)
                        except (OSError, ValueError, RuntimeError) as exc:
                            failures += 1  # One flaky `claude agents` call must not end a run.
                            note("agents", f"claude agents failed ({failures}): {exc}")
                            if failures >= 5:
                                raise RuntimeError(f"claude agents keeps failing: {exc}") from exc
                            await asyncio.sleep(0.1)
                            continue
                        failures = 0
                        observed = entry.get("status") if entry and entry.get("pid") else "exited"
                        if observed != status:
                            status = observed
                            note("agents", json.dumps(entry or {"id": short, "status": observed}))
                            if observed == "waiting":
                                service.add_comment(task["id"], "Claude is waiting at a permission prompt. "
                                                    f"Run `claude attach {short}` to answer it.", run_id=run["id"])
                        # The ACP tools answer before the turn can end: re-read the task after observing.
                        if service.store.one("SELECT state FROM tasks WHERE id=?", (task["id"],))["state"] in TERMINAL:
                            continue
                        if observed == "exited" and entry and entry.get("state") == "failed":
                            # Claude settled the session as failed; it can die before it ever starts.
                            detail = job_detail(short)
                            note("job", detail or "failed without a detail")
                            raise RuntimeError(f"Claude background session {short} failed"
                                               f"{': ' + detail if detail else ''}. See the run log.")
                        # Until its worker starts, Claude lists the session as working without a pid.
                        pending = entry and not entry.get("pid") and entry.get("state") == "working" and not seen_busy
                        starting = pending and time.monotonic() - started < STARTUP_LIMIT_SECONDS
                        misses = misses + 1 if observed == "exited" and not starting else 0
                        if starting or was_starting:
                            # Polls can be most of a grace apart, so the grace runs from the first
                            # poll that sees the session started, not the last that saw it starting.
                            grace_from = time.monotonic()
                        was_starting = bool(starting)
                        # Right after launch the registry can list the session before its pid, and
                        # idle before the prompt is picked up; a brief registry gap is not an exit either.
                        # A session that has not started by the startup limit gets no further grace.
                        settled = (seen_busy or (pending and not starting)
                                   or time.monotonic() - grace_from > STARTUP_GRACE_SECONDS)
                        if observed == "busy":
                            seen_busy = True
                        elif observed == "idle" and settled:
                            raise RuntimeError(f"Claude background session {short} ended its turn without "
                                               f"request_input or submit_for_review. Run `claude attach {short}` "
                                               f"to open the stopped session, or see the run log.")
                        elif misses >= 2 and settled:
                            raise RuntimeError(f"Claude background session {short} exited without reporting "
                                               f"through the ACP tools. See the run log, or "
                                               f"`claude --resume {session}`.")
                    await asyncio.sleep(0.1)
            finally:
                if short and await stop_session(settings, short, note):
                    # The run is over either way; a live session must not go unnoticed.
                    message = (f"Claude background session {short} is still running after `claude stop`. "
                               f"Stop it with `claude stop {short}` before retrying.")
                    with service.store.transaction() as db:
                        db.execute("UPDATE tasks SET last_error=? WHERE id=?", (message, task["id"]))
                    raise RuntimeError(message)


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
