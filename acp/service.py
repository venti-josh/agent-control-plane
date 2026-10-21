from __future__ import annotations

import hashlib
import fnmatch
import json
import re
import sqlite3
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse
from uuid import uuid4

from .config import PROVIDERS, STATES, Settings
from .evidence import snapshot_files
from .store import Store, encode, row

TRANSITIONS = {
    "planning": {"todo"},
    "todo": {"planning", "in_progress", "waiting_on_input"},
    "in_progress": {"planning", "todo", "waiting_on_input", "ready_for_review"},
    "waiting_on_input": {"planning", "todo", "in_progress", "done"},
    "ready_for_review": {"planning", "todo", "in_progress", "waiting_on_input", "deploy", "done"},
    "deploy": {"planning", "ready_for_review", "waiting_on_input", "done"},
    "done": {"planning", "todo"},
}
PR_PATTERN = re.compile(r"^https://github\.com/([\w.-]+/[\w.-]+)/pull/(\d+)/?$")
# Answers to a Codex approval blocker. Anything else declines and is forwarded as steering.
DECISIONS = {"accept": "accept", "accept for session": "acceptForSession", "decline": "decline"}


class DomainError(Exception):
    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def required(text: str, name: str = "Text") -> str:
    text = text.strip()
    if not text:
        raise DomainError(f"{name} must not be empty", 422)
    return text


def parse_pr(url: str) -> tuple[str, int]:
    match = PR_PATTERN.fullmatch(url)
    if not match:
        raise DomainError("PR output must be an https://github.com/owner/repo/pull/number URL", 422)
    return match[1], int(match[2])


class TaskService:
    """The only place that mutates tasks. UI, HTTP, MCP and integrations share it."""

    def __init__(self, store: Store, settings: Settings):
        self.store, self.settings = store, settings
        # Switching workspace against a database with existing links is never implicit.
        if settings.linear_workspace_id:
            with store.transaction() as db:
                previous = db.execute("SELECT value FROM metadata WHERE key='linear_workspace'").fetchone()
                if previous and previous[0] != settings.linear_workspace_id:
                    raise ValueError("This database is linked to another Linear workspace. Use a new DATA_DIR.")
                db.execute("INSERT OR IGNORE INTO metadata VALUES('linear_workspace',?)",
                           (settings.linear_workspace_id,))

    @staticmethod
    def _task(db: sqlite3.Connection, task_id: str) -> dict:
        task = row(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
        if not task:
            raise DomainError("Task not found", 404)
        return task

    def _command(self, key, operation, arguments, apply):
        """Commit the operation and its replay result in the same SQLite transaction."""
        if key is not None and (not key.strip() or len(key) > 200):
            raise DomainError('Idempotency key must contain 1 to 200 characters.', 422)
        fingerprint = hashlib.sha256(json.dumps(arguments, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
        with self.store.transaction() as db:
            if key:
                previous = db.execute('SELECT * FROM command_receipts WHERE key=?', (key,)).fetchone()
                if previous:
                    if previous['operation'] != operation or previous['fingerprint'] != fingerprint:
                        raise DomainError('Idempotency key was already used for a different command.')
                    return json.loads(previous['result'])
            result = apply(db)
            if key:
                db.execute('INSERT INTO command_receipts VALUES(?,?,?,?,?)',
                           (key, operation, fingerprint, encode(result), time.time()))
            return result

    @staticmethod
    def _check_run(db: sqlite3.Connection, task: dict, run_id: str | None) -> None:
        if not run_id:
            return
        run = row(db.execute("SELECT * FROM runs WHERE id=? AND task_id=?", (run_id, task["id"])).fetchone())
        if not run or run["status"] != "running" or run["stop_requested"] or task["state"] != "in_progress":
            raise DomainError("This run is no longer active; stop work and do not change the task")

    def _event(self, db: sqlite3.Connection, kind: str, task_id: str | None,
               data: dict, origin: str = "system") -> int:
        now = time.time()
        event_id = db.execute("INSERT INTO events(type,task_id,origin,data,created_at) VALUES(?,?,?,?,?)",
                              (kind, task_id, origin, encode(data), now)).lastrowid
        for sub in db.execute("SELECT * FROM subscriptions").fetchall():
            if any(fnmatch.fnmatchcase(kind, pattern) for pattern in json.loads(sub["types"])):
                Store.enqueue(db, "webhook", f"subscription:{sub['id']}:{event_id}",
                              {"subscription_id": sub["id"], "event_id": event_id})
        if self.settings.linear_api_key and origin != "linear" and task_id:
            task = self._task(db, task_id)
            if task["linear_id"] and not task['hidden']:
                if kind in {"task.state_changed", "task.updated"}:
                    Store.enqueue(db, "linear.sync", f"linear-state:{event_id}", {"task_id": task_id})
                if kind in {"comment.created", "comment.updated", "comment.deleted"}:
                    Store.enqueue(db, "linear.comment", f"linear-comment:{event_id}",
                                  {"task_id": task_id, "comment_id": data["comment_id"]})
        return event_id

    def _comment(self, db: sqlite3.Connection, task_id: str, body: str, author: str,
                 origin: str, in_reply_to: str | None = None, external_id: str | None = None) -> dict:
        required(body, "Comment")  # Preserve Markdown indentation (notably code examples).
        if external_id:
            existing = row(db.execute("SELECT * FROM comments WHERE external_id=?", (external_id,)).fetchone())
            if existing:
                return existing
        if in_reply_to and not db.execute("SELECT id FROM comments WHERE id=? AND task_id=? AND deleted=0",
                                          (in_reply_to, task_id)).fetchone():
            raise DomainError("Reply target does not belong to this task", 422)
        cid, now = str(uuid4()), time.time()
        db.execute("INSERT INTO comments(id,task_id,body,author,in_reply_to,external_id,created_at,updated_at,deleted) "
                   "VALUES(?,?,?,?,?,?,?,?,0)",
                   (cid, task_id, body, author, in_reply_to, external_id, now, now))
        if origin != 'linear' and self.settings.linear_api_key and self._task(db, task_id)['linear_id']:
            snapshot_files(db, cid, body)
        self._event(db, "comment.created", task_id,
                    {"comment_id": cid, "body": body, "author": author, "in_reply_to": in_reply_to}, origin)
        return dict(db.execute("SELECT * FROM comments WHERE id=?", (cid,)).fetchone())

    def _schedule(self, db: sqlite3.Connection, task_id: str, trigger: str, context: dict | None = None) -> str:
        task = self._task(db, task_id)
        if task['hidden']:
            raise DomainError('Re-add the acp label in Linear before running this task.')
        if task["state"] not in {"todo", "waiting_on_input", "ready_for_review", "in_progress"}:
            raise DomainError("This task is not in an executable lane")
        pending = row(db.execute("SELECT * FROM runs WHERE task_id=? AND status='queued'", (task_id,)).fetchone())
        item = {"trigger": trigger, **(context or {})}
        if pending:
            inputs = json.loads(pending["inputs"])
            if item not in inputs:
                inputs.append(item)
                db.execute("UPDATE runs SET inputs=? WHERE id=?", (encode(inputs), pending["id"]))
            return pending["id"]
        rid = str(uuid4())
        db.execute("INSERT INTO runs(id,task_id,status,trigger,inputs,created_at) VALUES(?,?,'queued',?,?,?)",
                   (rid, task_id, trigger, encode([item]), time.time()))
        self._event(db, "run.queued", task_id, {"run_id": rid, "trigger": trigger})
        return rid

    def _route_comment(self, db: sqlite3.Connection, task: dict, comment: dict) -> None:
        """Route only new human input; agent/mirror/edit filtering belongs to callers."""
        if task['hidden'] or task['state'] not in {'todo', 'in_progress', 'waiting_on_input', 'ready_for_review'}:
            return
        if task['state'] == 'in_progress':
            # A live app-server turn takes the comment as steering instead of a second run.
            active = db.execute("SELECT id FROM runs WHERE task_id=? AND status='running' "
                                "AND stop_requested=0 AND steerable=1", (task['id'],)).fetchone()
            if active:
                db.execute('INSERT OR IGNORE INTO run_inputs(comment_id,run_id) VALUES(?,?)',
                           (comment['id'], active['id']))
                self._event(db, 'run.input_pending', task['id'],
                            {'run_id': active['id'], 'comment_id': comment['id']})
                return
        if task['state'] == 'waiting_on_input' and self._answer_approval(db, task, comment):
            return
        trigger = 'input' if task['state'] == 'waiting_on_input' else 'comment'
        self._schedule(db, task['id'], trigger, {'comment_id': comment['id']})

    def _move(self, db: sqlite3.Connection, task: dict, state: str, origin: str,
              *, comment: str | None = None, internal: bool = False,
              run_id: str | None = None, expected_version: int | None = None,
              blocker_comment_id: str | None = None) -> dict:
        if task['hidden']:
            raise DomainError('Re-add the acp label in Linear before changing this task.')
        if state not in STATES:
            raise DomainError("Unknown state", 422)
        if expected_version is not None and task["version"] != expected_version:
            raise DomainError("Task changed since you opened it. Refresh and try again.")
        self._check_run(db, task, run_id)
        if run_id and state in {'waiting_on_input', 'ready_for_review'} and db.execute(
                "SELECT 1 FROM run_inputs i JOIN comments c ON c.id=i.comment_id "
                "WHERE i.run_id=? AND i.status='pending' AND c.deleted=0", (run_id,)).fetchone():
            raise DomainError('New comments are being delivered to you. Incorporate them, then retry.')
        if task["state"] == state:
            return task
        if state not in TRANSITIONS[task["state"]]:
            raise DomainError(f"Cannot move from {task['state']} to {state}")
        if state == "in_progress" and not internal:
            raise DomainError("Move to Todo to queue work; In Progress is set only after the runner starts")
        blocker = None
        if state == "waiting_on_input":
            # Both records are committed together; no path can create a silent blocker.
            if blocker_comment_id:
                existing = db.execute("SELECT body FROM comments WHERE id=? AND task_id=? AND deleted=0",
                                      (blocker_comment_id, task["id"])).fetchone()
                if not existing or not existing[0].strip():
                    raise DomainError("The blocking question must be a nonempty comment on this task", 422)
                blocker = blocker_comment_id
            else:
                required(comment or "", "Question")
                blocker = self._comment(db, task["id"], comment,
                                        "agent" if run_id else "system" if origin == "system" else "you", origin)["id"]
        elif comment:
            self._comment(db, task["id"], comment, "you" if origin != "agent" else "agent", origin)
        if task['state'] == 'in_progress' and state != 'in_progress':
            now = time.time()
            db.execute("UPDATE runs SET activity_finished_at=coalesce(activity_finished_at,?) "
                       "WHERE task_id=? AND status='running'", (now, task['id']))
            db.execute('UPDATE subagent_activity SET finished_at=? WHERE finished_at IS NULL '
                       "AND run_id IN (SELECT id FROM runs WHERE task_id=? AND status='running')",
                       (now, task['id']))
        old = task["state"]
        db.execute("UPDATE tasks SET state=?,blocker_comment_id=?,version=version+1,updated_at=? WHERE id=?",
                   (state, blocker, time.time(), task["id"]))
        # A user move cancels obsolete queued work and asks any live process to stop.
        # Keep running status until the process is actually reaped.
        if not run_id and not internal:
            for pending in db.execute("SELECT id FROM runs WHERE task_id=? AND status='queued'",
                                      (task["id"],)).fetchall():
                db.execute("UPDATE runs SET status='cancelled',finished_at=? WHERE id=?", (time.time(), pending[0]))
                self._event(db, "run.cancelled", task["id"], {"run_id": pending[0], "reason": "Task moved"})
            db.execute("UPDATE runs SET stop_requested=1 WHERE task_id=? AND status='running'", (task["id"],))
        self._event(db, "task.state_changed", task["id"],
                    {"from": old, "to": state, "version": task["version"] + 1, "comment_id": blocker}, origin)
        if state == "todo":
            self._schedule(db, task["id"], "todo")
        return self._task(db, task["id"])

    def create_task(self, title: str, description: str = "", provider: str | None = None,
                    repo_path: str | None = None, state: str = "planning",
                    idempotency_key: str | None = None) -> dict:
        command = dict(title=title, description=description, provider=provider, repo_path=repo_path, state=state)
        provider = provider or self.settings.default_provider
        if provider not in PROVIDERS:
            raise DomainError("Unknown agent provider", 422)
        if state not in {"planning", "todo"}:
            raise DomainError("New tasks start in Planning or Todo", 422)
        repo_path = repo_path if repo_path is not None else self.settings.default_repo
        if repo_path:
            repo_path = str(Path(repo_path).expanduser().resolve())
        def apply(db):
            tid, now = str(uuid4()), time.time()
            db.execute("INSERT INTO tasks(id,title,description,state,provider,repo_path,created_at,updated_at) "
                       "VALUES(?,?,?,'planning',?,?,?,?)",
                       (tid, required(title, "Title"), description, provider, repo_path or "", now, now))
            self._event(db, "task.created", tid, {"title": title, "provider": provider}, "api")
            task = self._task(db, tid)
            return self._move(db, task, "todo", "api") if state == "todo" else task
        return self._command(idempotency_key, 'create_task', command, apply)

    def get_task(self, task_id: str) -> dict:
        with self.store.connect() as db:
            task = self._task(db, task_id)
            task["comments"] = [dict(c) for c in db.execute(
                "SELECT * FROM comments WHERE task_id=? AND deleted=0 ORDER BY created_at", (task_id,))]
            task["artifacts"] = [dict(a) for a in db.execute(
                "SELECT * FROM artifacts WHERE task_id=? ORDER BY created_at", (task_id,))]
            task["runs"] = [dict(r) for r in db.execute(
                "SELECT * FROM runs WHERE task_id=? ORDER BY created_at DESC", (task_id,))]
            for run in task["runs"]:
                run["inputs"] = json.loads(run["inputs"])
            approval = row(db.execute("SELECT id,request FROM approvals WHERE comment_id=? AND status='pending'",
                                      (task["blocker_comment_id"] or '',)).fetchone())
            task["approval"] = approval and {"id": approval["id"], "method": json.loads(approval["request"])["method"],
                                             "decisions": list(DECISIONS)}
            return task

    def list_tasks(self) -> list[dict]:
        return self.store.all("SELECT t.*, r.status AS run_status, r.started_at AS run_started_at "
                              "FROM tasks t LEFT JOIN runs r ON r.id="
                              "(SELECT id FROM runs WHERE task_id=t.id ORDER BY created_at DESC LIMIT 1) "
                              "WHERE t.hidden=0 ORDER BY t.created_at DESC")

    def move_task(self, task_id: str, state: str, comment: str | None = None,
                  expected_version: int | None = None, origin: str = "api") -> dict:
        with self.store.transaction() as db:
            return self._move(db, self._task(db, task_id), state, origin,
                              comment=comment, expected_version=expected_version)

    def update_task(self, task_id: str, title: str | None = None, description: str | None = None,
                    provider: str | None = None, repo_path: str | None = None,
                    expected_version: int | None = None) -> dict:
        with self.store.transaction() as db:
            task = self._task(db, task_id)
            if expected_version is not None and task["version"] != expected_version:
                raise DomainError("Task changed; refresh first")
            values = {}
            if title is not None:
                values["title"] = required(title, "Title")
            if description is not None:
                values["description"] = description
            if provider is not None or repo_path is not None:
                if db.execute("SELECT 1 FROM runs WHERE task_id=? AND status IN ('running','queued')",
                              (task_id,)).fetchone():
                    raise DomainError("Stop queued/running work before changing its agent or repository")
                if provider is not None:
                    if provider not in PROVIDERS:
                        raise DomainError("Unknown provider", 422)
                    values.update(provider=provider, session_id=None)
                if repo_path is not None:
                    if task["workspace"]:
                        raise DomainError("A task with an existing worktree cannot change repository; create a new task")
                    values["repo_path"] = str(Path(repo_path).expanduser().resolve()) if repo_path else ""
            if values:
                db.execute("UPDATE tasks SET " + ",".join(f"{key}=?" for key in values) +
                           ",version=version+1,updated_at=? WHERE id=?", (*values.values(), time.time(), task_id))
                self._event(db, "task.updated", task_id, {"fields": list(values)}, "api")
            return self._task(db, task_id)

    def add_comment(self, task_id: str, body: str, in_reply_to: str | None = None,
                    run_id: str | None = None, resume: bool = False,
                    idempotency_key: str | None = None) -> dict:
        command = dict(task_id=task_id, body=body, in_reply_to=in_reply_to, run_id=run_id, resume=resume)
        def apply(db):
            reply_to = in_reply_to
            task = self._task(db, task_id)
            self._check_run(db, task, run_id)
            if resume and run_id:
                raise DomainError("Agent comments cannot resume themselves", 422)
            if resume and task["state"] != "waiting_on_input":
                raise DomainError("Only a waiting task can be resumed")
            if resume and not reply_to:
                raise DomainError("An explicit answer requires the expected blocker comment ID.", 422)
            wake = not run_id and task["state"] == "waiting_on_input"
            stale_reply = wake and reply_to is not None and reply_to != task["blocker_comment_id"]
            if wake and not reply_to:
                reply_to = task["blocker_comment_id"]
            comment = self._comment(db, task_id, body, "agent" if run_id else "you",
                                    "agent" if run_id else "api", reply_to)
            if not run_id and not stale_reply:
                self._route_comment(db, task, comment)
            if resume:
                comment["resume_queued"] = not stale_reply
                if stale_reply:
                    comment["notice"] = "Saved as context only: this answer refers to an earlier blocker. Answer the current question to resume."
            return comment
        return self._command(idempotency_key, 'add_comment', command, apply)

    def edit_comment(self, task_id: str, comment_id: str, body: str | None = None, delete: bool = False) -> dict:
        with self.store.transaction() as db:
            task = self._task(db, task_id)
            comment = row(db.execute("SELECT * FROM comments WHERE id=? AND task_id=? AND deleted=0",
                                     (comment_id, task_id)).fetchone())
            if not comment:
                raise DomainError("Comment not found", 404)
            if delete and task["state"] == "waiting_on_input" and task["blocker_comment_id"] == comment_id:
                raise DomainError("The current blocker cannot be deleted while the task is waiting")
            if not delete:
                required(body or "", "Comment")
                if self.settings.linear_api_key and task['linear_id']:
                    snapshot_files(db, comment_id, body)
            db.execute("UPDATE comments SET body=?,deleted=?,updated_at=? WHERE id=?",
                       (body if body is not None else comment["body"], int(delete), time.time(), comment_id))
            self._event(db, "comment.deleted" if delete else "comment.updated", task_id,
                        {"comment_id": comment_id, "body": body}, "api")
            return dict(db.execute("SELECT * FROM comments WHERE id=?", (comment_id,)).fetchone())

    def request_input(self, task_id: str, question: str, run_id: str | None = None,
                      idempotency_key: str | None = None) -> dict:
        command = dict(task_id=task_id, question=question, run_id=run_id)
        def apply(db):
            return self._move(db, self._task(db, task_id), "waiting_on_input",
                              "agent" if run_id else "api", comment=question, run_id=run_id)
        return self._command(idempotency_key, 'request_input', command, apply)

    def submit_for_review(self, task_id: str, summary: str, output_url: str = "", kind: str = "text",
                          run_id: str | None = None, idempotency_key: str | None = None) -> dict:
        command = dict(task_id=task_id, summary=summary, output_url=output_url, kind=kind, run_id=run_id)
        required(summary, "Summary")
        if kind not in {"text", "pr", "file", "url"}:
            raise DomainError("Output kind must be text, pr, file, or url", 422)
        if kind != "text":
            required(output_url, "Output URL or file path")
        if kind == "pr":
            parse_pr(output_url)
        if kind == "url" and urlparse(output_url).scheme not in {"https", "http"}:
            raise DomainError("URL outputs must use HTTP(S)", 422)
        def apply(db):
            task = self._task(db, task_id)
            self._check_run(db, task, run_id)
            if task["state"] != "in_progress":
                raise DomainError("Only executing work can be submitted for review")
            artifact_url = output_url or f"text:{uuid4()}"
            aid = str(uuid4())
            db.execute("INSERT INTO artifacts VALUES(?,?,?,?,?,?) ON CONFLICT(task_id,url) "
                       "DO UPDATE SET summary=excluded.summary,kind=excluded.kind",
                       (aid, task_id, kind, artifact_url, summary, time.time()))
            self._event(db, "artifact.submitted", task_id, {"kind": kind, "url": artifact_url, "summary": summary},
                        "agent" if run_id else "api")
            output_link = output_url
            if kind == 'file' and output_url and urlparse(output_url).scheme not in {'http', 'https'}:
                file_path = Path(output_url)
                if output_url.startswith('file://'):
                    from urllib.parse import unquote
                    parsed_file = urlparse(output_url)
                    if parsed_file.netloc not in {'', 'localhost'}:
                        raise DomainError('File review URLs must refer to the local host', 422)
                    file_path = Path(unquote(parsed_file.path))
                if not file_path.is_absolute():
                    file_path = Path(task['workspace'] or task['repo_path'] or '.') / file_path
                label = re.sub(r'([\\\[\]])', r'\\\1', file_path.name)
                output_link = f'[{label}](<{file_path.absolute().as_uri()}>)'
            self._comment(db, task_id, summary + (f"\n\n{output_link}" if output_link else ""),
                          "agent" if run_id else "you", "agent" if run_id else "api")
            return self._move(db, task, "ready_for_review", "agent" if run_id else "api", run_id=run_id)
        return self._command(idempotency_key, 'submit_for_review', command, apply)

    def request_changes(self, task_id: str, feedback: str, idempotency_key: str | None = None) -> dict:
        command = dict(task_id=task_id, feedback=feedback)
        def apply(db):
            task = self._task(db, task_id)
            if task["state"] != "ready_for_review":
                raise DomainError("Task is not ready for review")
            comment = self._comment(db, task_id, feedback, "you", "api")
            return {"run_id": self._schedule(db, task_id, "review", {"comment_id": comment["id"]})}
        return self._command(idempotency_key, 'request_changes', command, apply)

    def reset_session(self, task_id: str) -> dict:
        with self.store.transaction() as db:
            task = self._task(db, task_id)
            if db.execute("SELECT 1 FROM runs WHERE task_id=? AND status IN ('running','queued')", (task_id,)).fetchone():
                raise DomainError("Stop work before resetting the session")
            db.execute("UPDATE tasks SET session_id=NULL WHERE id=?", (task_id,))
            self._event(db, "session.reset", task_id, {}, "api")
            return self._task(db, task["id"])

    def cancel(self, task_id: str) -> dict:
        with self.store.transaction() as db:
            task = self._task(db, task_id)
            for run in db.execute("SELECT * FROM runs WHERE task_id=? AND status='queued'", (task_id,)).fetchall():
                db.execute("UPDATE runs SET status='cancelled',finished_at=? WHERE id=?", (time.time(), run["id"]))
                self._event(db, "run.cancelled", task_id, {"run_id": run["id"]}, "api")
            db.execute("UPDATE runs SET stop_requested=1 WHERE task_id=? AND status='running'", (task_id,))
            if task["state"] in {"todo", "in_progress"}:
                return self._move(db, task, "planning", "api")
            return task

    def claim_next(self) -> dict | None:
        with self.store.transaction() as db:
            run = row(db.execute("SELECT r.* FROM runs r JOIN tasks t ON t.id=r.task_id WHERE r.status='queued' "
                                 "AND t.hidden=0 AND t.state IN ('todo','waiting_on_input','ready_for_review','in_progress') "
                                 "AND NOT EXISTS(SELECT 1 FROM runs x WHERE x.task_id=r.task_id AND x.status='running') "
                                 "ORDER BY r.created_at LIMIT 1").fetchone())
            if not run:
                return None
            # 'running' here is the private run lifecycle. The visible lane changes in mark_started,
            # only after a provider process has launched (not merely when the job is claimed).
            db.execute("UPDATE runs SET status='running' WHERE id=?", (run["id"],))
            run["status"] = "running"
            run["inputs"] = json.loads(run["inputs"])
            return run

    def mark_started(self, run_id: str) -> None:
        with self.store.transaction() as db:
            run = row(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            if not run or run["status"] != "running" or run["stop_requested"]:
                raise DomainError("Run cancelled before launch")
            if run["started_at"] is not None:
                return
            task = self._task(db, run["task_id"])
            self._move(db, task, "in_progress", "system", internal=True)
            db.execute("UPDATE tasks SET last_error=NULL WHERE id=?", (task["id"],))
            db.execute("UPDATE runs SET started_at=? WHERE id=?", (time.time(), run_id))
            self._event(db, "run.started", task["id"], {"run_id": run_id})

    def remember_session(self, run_id: str, session_id: str) -> None:
        with self.store.transaction() as db:
            run = row(db.execute("SELECT * FROM runs WHERE id=? AND status='running'", (run_id,)).fetchone())
            if run and not run["stop_requested"]:
                db.execute("UPDATE tasks SET session_id=? WHERE id=?", (session_id, run["task_id"]))
                db.execute("UPDATE runs SET session_id=? WHERE id=?", (session_id, run_id))

    def finish_run(self, run_id: str, status: str = "succeeded", error: str | None = None) -> None:
        with self.store.transaction() as db:
            self._finish_run(db, run_id, status, error)

    def _finish_run(self, db, run_id, status='succeeded', error=None):
        run = row(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
        if not run or run["status"] != "running":
            return
        if run["stop_requested"]:
            status, error = "cancelled", None
        task = self._task(db, run["task_id"])
        if status == "succeeded" and task["state"] in {"todo", "in_progress"}:
            status, error = "failed", "Agent exited without request_input or submit_for_review. Inspect its log and retry."
        db.execute("UPDATE runs SET status=?,error=?,finished_at=? WHERE id=?",
                   (status, error, time.time(), run_id))
        db.execute('UPDATE subagent_activity SET finished_at=? WHERE run_id=? AND finished_at IS NULL',
                   (time.time(), run_id))
        self._event(db, f"run.{status}", task["id"], {"run_id": run_id, "error": error})
        db.execute("UPDATE approvals SET status='cancelled' WHERE run_id=? AND status IN ('pending','answered')",
                   (run_id,))
        # Steering the turn never received becomes a follow-up, like any comment on a busy task.
        undelivered = [r[0] for r in db.execute(
            "SELECT i.comment_id FROM run_inputs i JOIN comments c ON c.id=i.comment_id "
            "WHERE i.run_id=? AND i.status='pending' AND c.deleted=0 ORDER BY c.created_at", (run_id,))]
        db.execute("DELETE FROM run_inputs WHERE run_id=? AND status='pending'", (run_id,))
        task = self._task(db, task["id"])
        if status == "succeeded" and not task['hidden'] and task["state"] in {"waiting_on_input", "ready_for_review"}:
            for comment_id in undelivered:
                self._schedule(db, task["id"], "comment", {"comment_id": comment_id})
        if error:
            db.execute("UPDATE tasks SET last_error=? WHERE id=?", (error, task["id"]))
            if task["state"] in {"in_progress", "todo", "ready_for_review"}:
                self._move(db, task, "waiting_on_input", "system", comment=error, internal=True)
            else:
                self._comment(db, task["id"], error, "system", "system")
            # Failure requires an explicit human retry; don't consume an already queued reply.
            for queued in db.execute("SELECT id FROM runs WHERE task_id=? AND status='queued'", (task["id"],)):
                db.execute("UPDATE runs SET status='cancelled',finished_at=? WHERE id=?", (time.time(), queued[0]))
                self._event(db, "run.cancelled", task["id"], {"run_id": queued[0], "reason": "Previous run failed"})

    def _answer_approval(self, db, task: dict, comment: dict) -> bool:
        """Resolve a pending Codex approval from the human answer to its blocker."""
        approval = row(db.execute("SELECT a.* FROM approvals a JOIN runs r ON r.id=a.run_id "
                                  "WHERE a.comment_id=? AND a.status='pending' AND r.status='running' "
                                  "AND r.stop_requested=0", (task['blocker_comment_id'] or '',)).fetchone())
        if not approval:
            return False
        decision = DECISIONS.get(' '.join(comment['body'].lower().strip().rstrip('.!').split()))
        db.execute("UPDATE approvals SET decision=?,status='answered' WHERE id=?",
                   (decision or 'decline', approval['id']))
        if not decision:
            db.execute('INSERT OR IGNORE INTO run_inputs(comment_id,run_id) VALUES(?,?)',
                       (comment['id'], approval['run_id']))
        self._event(db, 'approval.answered', task['id'], {'run_id': approval['run_id'],
                    'approval_id': approval['id'], 'decision': decision or 'decline'})
        self._resume_run(db, task, approval['run_id'])
        return True

    def _resume_run(self, db, task: dict, run_id: str) -> None:
        # The run never stopped; leaving Waiting on Input must not end its activity interval.
        self._move(db, task, 'in_progress', 'system', internal=True)
        db.execute('UPDATE runs SET activity_finished_at=NULL WHERE id=?', (run_id,))

    def approval_resolved_elsewhere(self, approval_id: str) -> None:
        """Another client of a shared app-server answered the request (e.g. the Codex app)."""
        with self.store.transaction() as db:
            approval = row(db.execute("SELECT * FROM approvals WHERE id=? AND status IN ('pending','answered')",
                                      (approval_id,)).fetchone())
            if not approval:
                return
            db.execute("UPDATE approvals SET status='resolved' WHERE id=?", (approval_id,))
            run = row(db.execute("SELECT * FROM runs WHERE id=?", (approval['run_id'],)).fetchone())
            task = self._task(db, run['task_id'])
            if task['state'] == 'waiting_on_input' and task['blocker_comment_id'] == approval['comment_id']:
                self._comment(db, task['id'], 'Answered outside ACP.', 'system', 'system',
                              in_reply_to=approval['comment_id'])
                self._resume_run(db, task, run['id'])

    def request_approval(self, run_id: str, request: dict, question: str) -> str:
        """Park a live run on a Codex approval request: the task waits on a blocker comment."""
        with self.store.transaction() as db:
            run = row(db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone())
            task = self._task(db, run['task_id']) if run else None
            self._check_run(db, task or {'id': ''}, run_id)
            # Not the agent pausing: the run stays live, so pending steering need not block it.
            blocker = self._comment(db, task['id'], question, 'agent', 'agent')
            self._move(db, task, 'waiting_on_input', 'agent', internal=True, blocker_comment_id=blocker['id'])
            approval_id = str(uuid4())
            db.execute("INSERT INTO approvals VALUES(?,?,?,?,NULL,'pending',?)",
                       (approval_id, run_id, blocker['id'], encode(request), time.time()))
            return approval_id

    def answered_approvals(self, run_id: str) -> list[dict]:
        return self.store.all("SELECT * FROM approvals WHERE run_id=? AND status='answered'", (run_id,))

    def settle_approval(self, approval_id: str, status: str = 'resolved') -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE approvals SET status=? WHERE id=? AND status IN ('pending','answered')",
                       (status, approval_id))

    def pending_inputs(self, run_id: str) -> list[dict]:
        return self.store.all("SELECT c.* FROM run_inputs i JOIN comments c ON c.id=i.comment_id "
                              "WHERE i.run_id=? AND i.status='pending' AND c.deleted=0 ORDER BY c.created_at",
                              (run_id,))

    def input_delivered(self, comment_id: str) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE run_inputs SET status='delivered' WHERE comment_id=?", (comment_id,))

    def defer_input(self, comment_id: str) -> None:
        """Steering failed: queue the comment as a follow-up so the agent can still report."""
        with self.store.transaction() as db:
            pending = row(db.execute("SELECT i.run_id,c.task_id FROM run_inputs i JOIN comments c ON c.id=i.comment_id "
                                     "WHERE i.comment_id=? AND i.status='pending'", (comment_id,)).fetchone())
            if not pending:
                return
            db.execute("DELETE FROM run_inputs WHERE comment_id=?", (comment_id,))
            if self._task(db, pending['task_id'])['state'] in {'todo', 'in_progress', 'waiting_on_input', 'ready_for_review'}:
                self._schedule(db, pending['task_id'], 'comment', {'comment_id': comment_id})

    def mark_steerable(self, run_id: str) -> None:
        with self.store.transaction() as db:
            db.execute("UPDATE runs SET steerable=1 WHERE id=?", (run_id,))

    def recover(self) -> None:
        for run in self.store.all("SELECT id FROM runs WHERE status='running'"):
            self.finish_run(run["id"], "interrupted",
                            "The server stopped during this run. Its session and worktree are retained. "
                            "Check the log and retry when ready; the task was not automatically rerun.")
        with self.store.transaction() as db:
            db.execute("UPDATE outbox SET status='pending' WHERE status='working'")

    def subscribe(self, url: str, types: list[str], secret: str = "") -> dict:
        if urlparse(url).scheme not in {"http", "https"} or not urlparse(url).hostname:
            raise DomainError("Subscription URL must use HTTP(S)", 422)
        if not types or any(not x.strip() for x in types):
            raise DomainError("At least one event type or pattern is required", 422)
        sid = str(uuid4())
        with self.store.transaction() as db:
            db.execute("INSERT INTO subscriptions VALUES(?,?,?,?,?)", (sid, url, encode(types), secret, time.time()))
        return {"id": sid, "url": url, "types": types}

    def receive(self, source: str, delivery_id: str, payload: dict, kind: str) -> dict:
        """Persist incoming webhooks before acknowledging them; no network call in this transaction."""
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM receipts WHERE source=? AND id=?", (source, delivery_id)).fetchone():
                return {"accepted": True, "duplicate": True}
            db.execute("INSERT INTO receipts VALUES(?,?,?)", (source, delivery_id, time.time()))
            self._event(db, f"integration.{source}.received", None, {"delivery_id": delivery_id}, source)
            Store.enqueue(db, kind, f"inbox:{source}:{delivery_id}", payload)
        return {"accepted": True, "duplicate": False}

    def once(self, source: str, key: str, callback: Callable[[sqlite3.Connection], None]) -> bool:
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM receipts WHERE source=? AND id=?", (source, key)).fetchone():
                return False
            callback(db)
            db.execute("INSERT INTO receipts VALUES(?,?,?)", (source, key, time.time()))
            return True
