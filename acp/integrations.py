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
from .evidence import replace_links
from .service import DomainError, TaskService, parse_pr, required
from .store import Store, encode, row

MIRROR = re.compile(r"<!-- acp:([a-f0-9-]{36}) -->")
ISSUE_FIELDS = "id identifier url title description updatedAt archivedAt state { id } team { id } assignee { id } labels { nodes { name } }"


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
        if not self.settings.linear_enabled:
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

    async def fetch_linear_issue(self, issue_id: str, *, allow_other_team=False) -> dict:
        data = await self.linear("query($id:String!){organization{id} issue(id:$id){" + ISSUE_FIELDS + "}}",
                                 {"id": issue_id})
        if data["organization"]["id"] != self.settings.linear_workspace_id:
            raise DomainError("The Linear token belongs to a different workspace", 422)
        issue = data.get("issue")
        if not issue:
            raise DomainError("Linear issue not found", 404)
        if issue["team"]["id"] != self.settings.linear_team_id and not allow_other_team:
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
        if self.settings.linear_label_sync and not self.belongs_on_board(issue):
            raise DomainError('Assign this issue to the board owner and add the acp label before syncing it.', 422)
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
        if not task["linear_id"] or task['hidden']:
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
        if not task["linear_id"] or task['hidden'] or not comment:
            return
        remote_id = (comment["external_id"] or "").removeprefix("linear:")
        if comment["deleted"]:
            if remote_id:
                await self.linear("mutation($id:String!){commentDelete(id:$id){success}}", {"id": remote_id})
            return
        evidence_body = await self.upload_comment_files(comment_id, comment['body'])
        body = f"<!-- acp:{comment_id} -->\n**{comment['author']}**\n\n{evidence_body}"
        if remote_id:
            await self.linear("mutation($id:String!,$input:CommentUpdateInput!){commentUpdate(id:$id,input:$input)"
                              "{success}}", {"id": remote_id, "input": {"body": body}})
            return
        # Recover uncertain writes and find the original ACP thread, including after a restart.
        cursor, remote_comments = None, []
        while True:
            data = await self.linear("query($id:String!,$after:String){issue(id:$id){comments(first:100,after:$after)"
                                     "{nodes{id body parent{id}} pageInfo{hasNextPage endCursor}}}}",
                                     {"id": task["linear_id"], "after": cursor})
            connection = data["issue"]["comments"]
            remote_comments.extend(connection["nodes"])
            if not connection["pageInfo"]["hasNextPage"]:
                break
            cursor = connection["pageInfo"]["endCursor"]
        mirrored = {}
        for remote in remote_comments:
            marker = MIRROR.search(remote.get('body') or '')
            if marker:
                mirrored[marker[1]] = remote
        parent_id = None
        match = mirrored.get(comment_id)
        if match:
            remote_id = match['id']
        if not remote_id:
            remote_ids = {c['id'] for c in remote_comments}
            for local in task['comments']:
                prior = mirrored.get(local['id'])
                if prior:
                    root_id = (prior.get('parent') or {}).get('id') or prior['id']
                    if root_id in remote_ids:
                        parent_id = root_id
                        break
            create_input = {"issueId": task["linear_id"], "body": body}
            if parent_id:
                create_input['parentId'] = parent_id
            result = await self.linear("mutation($input:CommentCreateInput!){commentCreate(input:$input)"
                                       "{success comment{id}}}", {"input": create_input})
            remote_id = result["commentCreate"]["comment"]["id"]
        with self.store.transaction() as db:
            db.execute("UPDATE comments SET external_id=? WHERE id=?", (f"linear:{remote_id}", comment_id))

    async def upload_comment_files(self, comment_id, body):
        urls = {}
        for file in self.store.all('SELECT * FROM comment_files WHERE comment_id=? ORDER BY rowid',
                                   (comment_id,)):
            asset_url = file['asset_url']
            if not asset_url:
                data = await self.linear(
                    'mutation($contentType:String!,$filename:String!,$size:Int!){'
                    'fileUpload(contentType:$contentType,filename:$filename,size:$size)'
                    '{success uploadFile{uploadUrl assetUrl headers{key value}}}}',
                    {'contentType': file['content_type'], 'filename': file['filename'],
                     'size': len(file['content'])})
                upload = data['fileUpload']['uploadFile']
                headers = {'Content-Type': file['content_type'], 'Cache-Control': 'public, max-age=31536000'}
                headers.update({h['key']: h['value'] for h in upload['headers']})
                response = await self.client.put(upload['uploadUrl'], headers=headers, content=file['content'])
                response.raise_for_status()
                asset_url = upload['assetUrl']
                with self.store.transaction() as db:
                    db.execute('UPDATE comment_files SET asset_url=? WHERE comment_id=? AND target=? AND digest=?',
                               (asset_url, comment_id, file['target'], file['digest']))
            urls[file['target']] = asset_url
        return replace_links(body, urls)

    @staticmethod
    def labelled(issue):
        return not issue.get('archivedAt') and any(label['name'].casefold() == 'acp'
                                                   for label in issue.get('labels', {}).get('nodes', []))

    def belongs_on_board(self, issue):
        return (self.labelled(issue) and issue['team']['id'] == self.settings.linear_team_id
                and (not self.settings.linear_user_id
                     or (issue.get('assignee') or {}).get('id') == self.settings.linear_user_id))

    def hide_linear(self, task_id):
        with self.store.transaction() as db:
            task = self.service._task(db, task_id)
            if task['hidden']:
                return
            db.execute('UPDATE tasks SET hidden=1,version=version+1 WHERE id=?', (task_id,))
            db.execute("UPDATE runs SET stop_requested=1 WHERE task_id=? AND status='running'", (task_id,))
            db.execute("UPDATE runs SET status='cancelled',finished_at=? WHERE task_id=? AND status='queued'",
                       (time.time(), task_id))
            # Do not replay stale writes if the label is re-added later.
            db.execute("UPDATE outbox SET status='done' WHERE kind IN ('linear.sync','linear.comment') "
                       "AND status IN ('pending','failed') AND json_extract(payload,'$.task_id')=?", (task_id,))
            self.service._event(db, 'task.hidden', task_id, {'reason': 'Issue no longer matches the board owner, team, label or statuses'}, 'linear')

    async def sync_labelled(self):
        if not self.settings.linear_label_sync or not self.settings.linear_enabled:
            return {'synced': 0}
        cursor, ids = None, set()
        while True:
            query_filter = 'labels:{some:{name:{eqIgnoreCase:"acp"}}}'
            if self.settings.linear_user_id:
                query_filter += ',assignee:{id:{eq:$owner}}'
            owner_argument = ',$owner:ID!' if self.settings.linear_user_id else ''
            data = await self.linear('query ACPLabelled($team:String!,$after:String' + owner_argument + '){organization{id} team(id:$team){'
                                     'issues(first:100,after:$after,filter:{' + query_filter + '})'
                                     '{nodes{id} pageInfo{hasNextPage endCursor}}}}',
                                     {'team': self.settings.linear_team_id, 'after': cursor,
                                      **({'owner': self.settings.linear_user_id} if self.settings.linear_user_id else {})})
            if data['organization']['id'] != self.settings.linear_workspace_id:
                raise DomainError('Linear API key belongs to another workspace', 422)
            connection = data['team']['issues']
            ids.update(i['id'] for i in connection['nodes'])
            if not connection['pageInfo']['hasNextPage']:
                break
            cursor = connection['pageInfo']['endCursor']
        ids.update(t['linear_id'] for t in self.store.all('SELECT linear_id FROM tasks WHERE linear_id IS NOT NULL AND hidden=0'))
        for issue_id in ids:
            await self.handle_linear({'organizationId': self.settings.linear_workspace_id,
                                      'type': 'Issue', 'action': 'update', 'data': {'id': issue_id}})
            task = self.store.one('SELECT id FROM tasks WHERE linear_id=? AND hidden=0', (issue_id,))
            if task:
                await self.poll_comments(task['id'])
        return {'synced': len(ids)}

    async def poll_comments(self, task_id):
        task = self.service.get_task(task_id)
        if task['hidden']:
            return
        cursor, comments = None, []
        while True:
            data = await self.linear('query ACPComments($id:String!,$after:String){issue(id:$id){'
                                     'comments(first:100,after:$after){nodes{id body createdAt updatedAt '
                                     'user{name} botActor{__typename} parent{id}} pageInfo{hasNextPage endCursor}}}}',
                                     {'id': task['linear_id'], 'after': cursor})
            if not data.get('issue'):
                return
            connection = data['issue']['comments']
            comments.extend(connection['nodes'])
            if not connection['pageInfo']['hasNextPage']:
                break
            cursor = connection['pageInfo']['endCursor']
        cutoff = max(task['created_at'], task['linear_watch_since'])
        for comment in sorted(comments, key=lambda c: timestamp(c['createdAt'])):
            if timestamp(comment['createdAt']) < cutoff:
                continue
            # An edited comment first seen after downtime is context, never a new command.
            # Linear can stamp updatedAt slightly before createdAt on creation.
            action = 'update' if timestamp(comment['updatedAt']) > timestamp(comment['createdAt']) else 'create'
            human = comment.get('user') and not comment.get('botActor')
            await self.handle_linear({'organizationId': self.settings.linear_workspace_id,
                                      'type': 'Comment', 'action': action,
                                      'actor': {'type': 'user' if human else 'integration',
                                                'name': (comment.get('user') or {}).get('name', 'Linear')},
                                      'data': {**comment, 'issueId': task['linear_id'],
                                               'parentId': (comment.get('parent') or {}).get('id')}})

    async def handle_linear(self, payload: dict) -> None:
        data = payload.get("data", {})
        if payload.get("organizationId") != self.settings.linear_workspace_id:
            return
        if payload.get('type') == 'Issue' and payload.get('action') == 'remove':
            local = self.store.one('SELECT id FROM tasks WHERE linear_id=?', (data.get('id'),))
            if local and self.settings.linear_label_sync:
                self.hide_linear(local['id'])
            return
        if payload.get("type") == "Issue" and payload.get("action") in {"create", "update"}:
            local = self.store.one("SELECT * FROM tasks WHERE linear_id=?", (data.get("id"),))
            if not local and not self.settings.linear_label_sync:
                return
            # A workspace webhook can include other teams. Ignore those before fetching.
            if not local and data.get('teamId') and data['teamId'] != self.settings.linear_team_id:
                return
            try:
                issue = await self.fetch_linear_issue(data["id"], allow_other_team=self.settings.linear_label_sync)
            except DomainError as exc:
                if exc.status == 404 and local and self.settings.linear_label_sync:
                    self.hide_linear(local['id'])
                    return
                raise
            if self.settings.linear_label_sync:
                if (not self.belongs_on_board(issue)
                        or issue['state']['id'] not in self.settings.linear_states.values()):
                    if local:
                        self.hide_linear(local['id'])
                    return
                if not local:
                    await self.import_linear(issue['id'], provider=self.settings.linear_provider,
                                             question='This issue is waiting for input in Linear. What is needed to proceed?')
                    return
            with self.store.transaction() as db:
                task = self.service._task(db, local["id"])
                if task['hidden']:
                    active = db.execute("SELECT 1 FROM runs WHERE task_id=? AND status='running'", (task['id'],)).fetchone()
                    if active:
                        return  # Wait for cancellation to finish; the next scan restores the card.
                    state = self.lane(issue['state']['id'])
                    state = 'todo' if state == 'in_progress' else state
                    db.execute('UPDATE tasks SET hidden=0,state=?,linear_updated_at=NULL,linear_watch_since=? WHERE id=?',
                               (state, timestamp(issue['updatedAt']), task['id']))
                    if state == 'todo':
                        self.service._schedule(db, task['id'], 'linear-label-added')
                    if state == 'waiting_on_input' and not task['blocker_comment_id']:
                        comment = self.service._comment(db, task['id'], 'What input is needed to proceed?', 'system', 'linear')
                        db.execute('UPDATE tasks SET blocker_comment_id=? WHERE id=?', (comment['id'], task['id']))
                    self.service._event(db, 'task.restored', task['id'], {}, 'linear')
                    task = self.service._task(db, task['id'])
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
        if not local or local['hidden']:
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
            if data.get("createdAt"):
                # Preserve remote ordering when a question and answer arrive in the same poll.
                comment["created_at"] = timestamp(data["createdAt"])
                db.execute("UPDATE comments SET created_at=? WHERE id=?", (comment["created_at"], comment["id"]))
            if action != "create" or (payload.get("actor") or {}).get("type", "").lower() != "user":
                return
            command, _, message = body.partition(" ")
            if task["state"] == "waiting_on_input":
                blocker = db.execute("SELECT created_at FROM comments WHERE id=?",
                                     (task["blocker_comment_id"],)).fetchone()
                if blocker and comment["created_at"] < blocker["created_at"]:
                    return  # A delayed earlier note is context, not an answer to this question.
                db.execute("UPDATE comments SET in_reply_to=? WHERE id=?", (task["blocker_comment_id"], comment["id"]))
                if not self.service._answer_approval(db, task, comment):
                    self.service._schedule(db, task["id"], "input", {"comment_id": comment["id"]})
            elif command == "/wait" and message.strip():
                try:
                    self.service._move(db, task, "waiting_on_input", "linear", blocker_comment_id=comment["id"])
                except DomainError as exc:
                    # Keep the human's comment even if its command is invalid in this lane.
                    self.service._comment(db, task["id"], str(exc), "system", "system")
                else:
                    Store.enqueue(db, "linear.sync", f"linear-wait:{comment['id']}", {"task_id": task["id"]})
            elif command == "/changes" and message.strip() and task["state"] == "ready_for_review":
                self.service._schedule(db, task["id"], "review", {"comment_id": comment["id"]})
            else:
                self.service._route_comment(db, task, comment)

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
        if kind == 'linear.scan':
            await self.sync_labelled()
        elif kind == "linear.sync":
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
