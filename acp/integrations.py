from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
from datetime import datetime
from uuid import uuid4

import httpx

from .config import PROVIDERS
from .service import DomainError, TaskService, parse_pr, required
from .store import Store, encode, row

MIRROR = re.compile(r"<!-- acp:([a-f0-9-]{36}) -->")
ISSUE_FIELDS = "id identifier url title description updatedAt state { id } team { id }"


def timestamp(value: str | None) -> float:
    if not value:
        return 0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def verify_signature(raw: bytes, secret: str, signature: str, prefix: str = "") -> None:
    if not secret:
        raise DomainError("This webhook integration has not been configured", 503)
    expected = prefix + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, signature or ""):
        raise DomainError("Invalid webhook signature", 401)


class Integrations:
    def __init__(self, service: TaskService, client: httpx.AsyncClient):
        self.service, self.store, self.settings, self.client = service, service.store, service.settings, client

    async def linear(self, query: str, variables: dict | None = None) -> dict:
        if not self.settings.linear_api_key:
            raise DomainError("Configure Linear credentials and the state mapping first", 503)
        response = await self.client.post("https://api.linear.app/graphql",
                                          headers={"Authorization": self.settings.linear_api_key},
                                          json={"query": query, "variables": variables or {}})
        response.raise_for_status()
        result = response.json()
        if result.get("errors"):
            raise RuntimeError("Linear: " + "; ".join(e.get("message", "GraphQL error") for e in result["errors"]))
        data = result["data"]
        for value in data.values():
            if isinstance(value, dict) and value.get("success") is False:
                raise RuntimeError("Linear mutation was not successful")
        return data

    async def fetch_linear_issue(self, issue_id: str) -> dict:
        data = await self.linear("query($id:String!){organization{id} issue(id:$id){" + ISSUE_FIELDS + "}}",
                                 {"id": issue_id})
        if data["organization"]["id"] != self.settings.linear_workspace_id:
            raise DomainError("The Linear token belongs to a different workspace", 422)
        issue = data.get("issue")
        if not issue:
            raise DomainError("Linear issue not found", 404)
        if issue["team"]["id"] != self.settings.linear_team_id:
            raise DomainError("Issue is outside the configured Linear team", 422)
        return issue

    def lane(self, state_id: str) -> str:
        for lane, remote in self.settings.linear_states.items():
            if remote == state_id:
                return lane
        raise DomainError("This Linear state is not mapped to a board lane", 422)

    async def import_linear(self, issue_id: str, provider: str | None = None,
                            repo_path: str | None = None, question: str | None = None) -> dict:
        issue = await self.fetch_linear_issue(issue_id)
        provider = provider or self.settings.default_provider
        if provider not in PROVIDERS:
            raise DomainError("Unknown provider", 422)
        state = self.lane(issue["state"]["id"])
        if state == "in_progress":
            state = "todo"  # The local runner, not the imported label, owns actual execution.
        if state == "waiting_on_input":
            question = required(question or "", "A question is required to import a waiting issue")
        with self.store.transaction() as db:
            existing = row(db.execute("SELECT * FROM tasks WHERE linear_id=?", (issue["id"],)).fetchone())
            if existing:
                return existing
            tid, now = str(uuid4()), time.time()
            db.execute("INSERT INTO tasks(id,title,description,state,provider,repo_path,linear_id,linear_url,"
                       "linear_updated_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (tid, issue["title"], issue.get("description") or "", state, provider,
                        repo_path if repo_path is not None else self.settings.default_repo,
                        issue["id"], issue["url"], issue["updatedAt"], now, now))
            self.service._event(db, "task.created", tid, {"title": issue["title"], "linear_id": issue["id"]}, "linear")
            if state == "waiting_on_input":
                comment = self.service._comment(db, tid, question, "you", "api")
                db.execute("UPDATE tasks SET blocker_comment_id=? WHERE id=?", (comment["id"], tid))
            self.service._event(db, "task.state_changed", tid, {"from": None, "to": state, "version": 1}, "linear")
            if state == "todo":
                self.service._schedule(db, tid, "todo")
            return self.service._task(db, tid)

    async def sync_linear_task(self, task_id: str) -> None:
        task = self.service.get_task(task_id)
        if not task["linear_id"]:
            return
        data = await self.linear(
            "mutation($id:String!,$input:IssueUpdateInput!){issueUpdate(id:$id,input:$input)"
            "{success issue{updatedAt}}}",
            {"id": task["linear_id"], "input": {"stateId": self.settings.linear_states[task["state"]],
                                               "title": task["title"], "description": task["description"]}})
        updated_at = data["issueUpdate"]["issue"]["updatedAt"]
        with self.store.transaction() as db:
            current = self.service._task(db, task_id)
            if timestamp(updated_at) > timestamp(current["linear_updated_at"]):
                db.execute("UPDATE tasks SET linear_updated_at=? WHERE id=?", (updated_at, task_id))

    async def sync_linear_comment(self, task_id: str, comment_id: str) -> None:
        task = self.service.get_task(task_id)
        comment = self.store.one("SELECT * FROM comments WHERE id=? AND task_id=?", (comment_id, task_id))
        if not task["linear_id"] or not comment:
            return
        remote_id = (comment["external_id"] or "").removeprefix("linear:")
        if comment["deleted"]:
            if remote_id:
                await self.linear("mutation($id:String!){commentDelete(id:$id){success}}", {"id": remote_id})
            return
        body = f"<!-- acp:{comment_id} -->\n**{comment['author']}**\n\n{comment['body']}"
        if remote_id:
            await self.linear("mutation($id:String!,$input:CommentUpdateInput!){commentUpdate(id:$id,input:$input)"
                              "{success}}", {"id": remote_id, "input": {"body": body}})
            return
        # Find a prior successful write after an uncertain network response. Do not blindly repost.
        cursor = None
        while True:
            data = await self.linear("query($id:String!,$after:String){issue(id:$id){comments(first:100,after:$after)"
                                     "{nodes{id body} pageInfo{hasNextPage endCursor}}}}",
                                     {"id": task["linear_id"], "after": cursor})
            connection = data["issue"]["comments"]
            match = next((c for c in connection["nodes"] if f"<!-- acp:{comment_id} -->" in c["body"]), None)
            if match:
                remote_id = match["id"]
                break
            if not connection["pageInfo"]["hasNextPage"]:
                break
            cursor = connection["pageInfo"]["endCursor"]
        if not remote_id:
            result = await self.linear("mutation($input:CommentCreateInput!){commentCreate(input:$input)"
                                       "{success comment{id}}}", {"input": {"issueId": task["linear_id"], "body": body}})
            remote_id = result["commentCreate"]["comment"]["id"]
        with self.store.transaction() as db:
            db.execute("UPDATE comments SET external_id=? WHERE id=?", (f"linear:{remote_id}", comment_id))

    async def handle_linear(self, payload: dict) -> None:
        data = payload.get("data", {})
        if payload.get("organizationId") != self.settings.linear_workspace_id:
            return
        if payload.get("type") == "Issue" and payload.get("action") in {"create", "update"}:
            # Only explicitly imported issues can execute, never the whole workspace.
            local = self.store.one("SELECT * FROM tasks WHERE linear_id=?", (data.get("id"),))
            if not local:
                return
            issue = await self.fetch_linear_issue(data["id"])
            with self.store.transaction() as db:
                task = self.service._task(db, local["id"])
                if timestamp(issue["updatedAt"]) <= timestamp(task["linear_updated_at"]):
                    return
                # Local changes that have not reached Linear take precedence over an inbound snapshot.
                pending = db.execute("SELECT 1 FROM outbox WHERE kind='linear.sync' AND status IN ('pending','working') "
                                     "AND json_extract(payload,'$.task_id')=?", (task["id"],)).fetchone()
                if pending:
                    return
                fields_changed = (task["title"] != issue["title"] or
                                  task["description"] != (issue.get("description") or ""))
                if fields_changed:
                    db.execute("UPDATE tasks SET title=?,description=?,version=version+1,updated_at=? WHERE id=?",
                               (issue["title"], issue.get("description") or "", time.time(), task["id"]))
                    self.service._event(db, "task.updated", task["id"], {"fields": ["title", "description"]}, "linear")
                db.execute("UPDATE tasks SET linear_updated_at=? WHERE id=?", (issue["updatedAt"], task["id"]))
                task = self.service._task(db, task["id"])
                desired = self.lane(issue["state"]["id"])
                if desired == task["state"]:
                    return
                if desired == "in_progress":
                    desired = "todo"
                try:
                    if desired == "waiting_on_input":
                        raise DomainError("Use a Linear comment `/wait your question` instead of a silent move to Waiting on Input.")
                    self.service._move(db, task, desired, "linear")
                except DomainError as exc:
                    self.service._comment(db, task["id"], str(exc), "system", "system")
                    Store.enqueue(db, "linear.sync", f"linear-correction:{uuid4()}", {"task_id": task["id"]})
            return
        if payload.get("type") != "Comment":
            return
        local = self.store.one("SELECT * FROM tasks WHERE linear_id=?", (data.get("issueId"),))
        if not local:
            return
        action = payload.get("action")
        external_id = f"linear:{data.get('id')}"
        with self.store.transaction() as db:
            task = self.service._task(db, local["id"])
            mirror = MIRROR.search(data.get("body") or "")
            if mirror:
                existing = db.execute("SELECT id FROM comments WHERE id=? AND task_id=?",
                                      (mirror[1], task["id"])).fetchone()
                if existing:
                    db.execute("UPDATE comments SET external_id=? WHERE id=?", (external_id, mirror[1]))
                    return
            existing = row(db.execute("SELECT * FROM comments WHERE external_id=?", (external_id,)).fetchone())
            if action == "remove":
                if existing and existing["id"] != task["blocker_comment_id"]:
                    db.execute("UPDATE comments SET deleted=1,updated_at=? WHERE id=?", (time.time(), existing["id"]))
                    self.service._event(db, "comment.deleted", task["id"], {"comment_id": existing["id"]}, "linear")
                elif existing:
                    # Retain the blocking question in local history; deletion cannot erase its reason.
                    self.service._event(db, "integration.linear.blocker_retained", task["id"],
                                        {"comment_id": existing["id"]}, "linear")
                return
            body = (data.get("body") or "").strip()
            if not body:
                return
            if existing:
                if existing["body"] != body:
                    db.execute("UPDATE comments SET body=?,updated_at=? WHERE id=?", (body, time.time(), existing["id"]))
                    self.service._event(db, "comment.updated", task["id"],
                                        {"comment_id": existing["id"], "body": body}, "linear")
                return  # Edits never wake an agent.
            author = (payload.get("actor") or {}).get("name", "Linear")
            comment = self.service._comment(db, task["id"], body, author, "linear", external_id=external_id)
            if action != "create" or (payload.get("actor") or {}).get("type", "").lower() != "user":
                return
            command, _, message = body.partition(" ")
            if command == "/wait" and message.strip():
                try:
                    self.service._move(db, task, "waiting_on_input", "linear", blocker_comment_id=comment["id"])
                except DomainError as exc:
                    # Keep the human's comment even if its command is invalid in this lane.
                    self.service._comment(db, task["id"], str(exc), "system", "system")
                else:
                    Store.enqueue(db, "linear.sync", f"linear-wait:{comment['id']}", {"task_id": task["id"]})
            elif command == "/resume" and message.strip() and task["state"] == "waiting_on_input":
                db.execute("UPDATE comments SET in_reply_to=? WHERE id=?", (task["blocker_comment_id"], comment["id"]))
                self.service._schedule(db, task["id"], "input", {"comment_id": comment["id"]})
            elif command == "/changes" and message.strip() and task["state"] == "ready_for_review":
                self.service._schedule(db, task["id"], "review", {"comment_id": comment["id"]})
            elif task["state"] == "waiting_on_input" and data.get("parentId"):
                parent = db.execute("SELECT external_id FROM comments WHERE id=?", (task["blocker_comment_id"],)).fetchone()
                if parent and parent[0] == f"linear:{data['parentId']}":
                    db.execute("UPDATE comments SET in_reply_to=? WHERE id=?", (task["blocker_comment_id"], comment["id"]))
                    self.service._schedule(db, task["id"], "input", {"comment_id": comment["id"]})

    async def github_get(self, path: str, params: dict | None = None) -> dict | list:
        headers = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        if self.settings.github_token:
            headers["Authorization"] = f"Bearer {self.settings.github_token}"
        response = await self.client.get(f"https://api.github.com/{path}", headers=headers, params=params)
        response.raise_for_status()
        return response.json()

    async def handle_github(self, envelope: dict) -> None:
        event, payload = envelope["event"], envelope["payload"]
        pr = payload.get("pull_request") or {}
        url = pr.get("html_url", "").rstrip("/")
        linked = self.store.one("SELECT t.* FROM tasks t JOIN artifacts a ON a.task_id=t.id "
                                "WHERE a.kind='pr' AND rtrim(a.url,'/')=?", (url,))
        if not linked:
            return
        repo, number = parse_pr(url)
        review = payload.get("review") or {}
        if event == "pull_request_review" and payload.get("action") == "submitted":
            key = f"{repo}:{number}:{review.get('id')}:submitted"
            if self.store.one("SELECT 1 FROM receipts WHERE source='github.review' AND id=?", (key,)):
                return
            state = review.get("state", "").lower()
            inline = []
            if state == "changes_requested":
                review_id = int(review["id"])
                page = 1
                while True:
                    batch = await self.github_get(f"repos/{repo}/pulls/{number}/reviews/{review_id}/comments",
                                                  {"per_page": 100, "page": page})
                    inline.extend({"path": c.get("path"), "line": c.get("line") or c.get("original_line"),
                                   "body": c.get("body"), "diff_hunk": c.get("diff_hunk"), "url": c.get("html_url")}
                                  for c in batch)
                    if len(batch) < 100:
                        break
                    page += 1

            def apply(db):
                task = self.service._task(db, linked["id"])
                self.service._event(db, "pr.review_submitted", task["id"],
                                    {"state": state, "review_id": review.get("id"), "url": url}, "github")
                if state == "changes_requested":
                    # Discard requests delivered after a task was intentionally closed or deployed.
                    if task["state"] not in {"ready_for_review", "in_progress", "waiting_on_input"}:
                        return
                    body = review.get("body") or "Changes requested; see the inline review feedback."
                    self.service._comment(db, task["id"], f"GitHub changes requested:\n{body}\n\n{review.get('html_url', url)}"
                                          + ("\n\nInline feedback:\n" + encode(inline) if inline else ""),
                                          "reviewer", "github")
                    # A review arriving during an unrelated blocker is saved, not treated as its answer.
                    if task["state"] != "waiting_on_input":
                        self.service._schedule(db, task["id"], "review",
                                               {"review_id": review["id"], "body": body, "inline_comments": inline,
                                                "pr_url": url, "commit_id": review.get("commit_id")})
            self.service.once("github.review", key, apply)
        elif event == "pull_request" and payload.get("action") == "closed":
            def close(db):
                task = self.service._task(db, linked["id"])
                self.service._event(db, "pr.merged" if pr.get("merged") else "pr.closed", task["id"], {"url": url}, "github")
                if pr.get("merged") and task["state"] == "ready_for_review":
                    self.service._move(db, task, "deploy", "github")
                else:
                    self.service._comment(db, task["id"],
                                          "The linked PR was closed. Check its outcome before continuing this task.",
                                          "system", "github")
            self.service.once("github.close", f"{repo}:{number}:{pr.get('closed_at')}", close)

    async def deliver(self, item: dict) -> None:
        payload, kind = json.loads(item["payload"]), item["kind"]
        if kind == "linear.sync":
            await self.sync_linear_task(payload["task_id"])
        elif kind == "linear.comment":
            await self.sync_linear_comment(payload["task_id"], payload["comment_id"])
        elif kind == "linear.incoming":
            await self.handle_linear(payload)
        elif kind == "github.incoming":
            await self.handle_github(payload)
        elif kind == "webhook":
            subscription = self.store.one("SELECT * FROM subscriptions WHERE id=?", (payload["subscription_id"],))
            if not subscription:
                return
            event = self.store.events(after=payload["event_id"] - 1, limit=1)[0]
            body = encode(event).encode()
            headers = {"Content-Type": "application/json", "X-ACP-Event": event["type"],
                       "X-ACP-Delivery": str(item["id"]), "X-ACP-Event-ID": str(event["id"])}
            if subscription["secret"]:
                headers["X-ACP-Signature"] = "sha256=" + hmac.new(
                    subscription["secret"].encode(), body, hashlib.sha256).hexdigest()
            response = await self.client.post(subscription["url"], content=body, headers=headers)
            response.raise_for_status()
        else:
            raise RuntimeError(f"Unknown outbox kind: {kind}")
