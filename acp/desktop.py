"""Supported Codex desktop bridge: a connected app chat consumes queued work."""
from __future__ import annotations

import asyncio
import json
import os
import shlex
import sys
import time
from pathlib import Path
from uuid import UUID

from .config import config_path
from .runners import CLIRunner, build_prompt
from .service import DomainError


def thread_id(value):
    try:
        return str(UUID(value))
    except (ValueError, TypeError, AttributeError):
        raise DomainError('Use the Codex chat UUID, not its title.', 422)


class DesktopBridge:
    def __init__(self, service):
        self.service, self.store = service, service.store

    def status(self):
        saved = self.store.one("SELECT value FROM metadata WHERE key='codex_dispatcher'")
        return {'connected': bool(saved), 'thread_id': saved['value'] if saved else None,
                'instructions': self.setup_prompt()}

    def command(self):
        # Pin this installation and configuration; the app's shell PATH may differ.
        return shlex.join(['env', f'ACP_CONFIG_FILE={config_path()}',
                           f'ACP_DATA_DIR={self.service.settings.data_dir}',
                           sys.executable, '-I', '-m', 'acp.cli', 'desktop',
                           '--url', self.service.settings.base_url])

    def setup_prompt(self):
        return ('Create a dedicated projectless Codex app chat titled ACP dispatcher. '
                'Its instructions: "You are the ACP dispatcher. The user authorizes you to '
                'create and continue Codex app chats for runs explicitly dispatched by their '
                'local ACP board. Use only supported app tools. Never execute task work yourself. '
                'Follow each queued run instruction, and never create duplicate chats." '
                'After create_thread returns its threadId, connect it by running: '
                f'{self.command()} connect --thread THREAD_ID. Do not use a queued clientThreadId. '
                'Keep the Codex app open while ACP is working.')

    def connect(self, value):
        value = thread_id(value) if value else None
        with self.store.transaction() as db:
            if db.execute("SELECT 1 FROM desktop_runs WHERE phase IN ('claimed','attached') OR "
                          "(phase='dispatched' AND run_id IN (SELECT id FROM runs WHERE status='running'))").fetchone():
                raise DomainError('Wait for current desktop runs before changing the connection.')
            if value:
                db.execute("INSERT OR REPLACE INTO metadata VALUES('codex_dispatcher',?)", (value,))
            else:
                db.execute("DELETE FROM metadata WHERE key='codex_dispatcher'")
        return self.status()

    def _require_active(self, db, run_id):
        run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
        if not run:
            raise DomainError('Unknown run', 404)
        task = self.service._task(db, run['task_id'])
        if run['status'] != 'running' or run['stop_requested'] or task.get('hidden'):
            raise DomainError('This run is no longer active; stop work and do not change the task')
        if task['provider'] != 'codex':
            raise DomainError('This run does not use Codex')
        return task, dict(run)

    def active(self, run_id):
        with self.store.connect() as db:
            task, run = self._require_active(db, run_id)
        return self.service.get_task(task['id']), run

    def dispatch(self, run_id):
        task, _ = self.active(run_id)
        with self.store.transaction() as db:
            self._require_active(db, run_id)
            if db.execute("SELECT 1 FROM desktop_runs d JOIN runs r ON r.id=d.run_id "
                          "WHERE r.task_id=? AND r.id<>? AND d.phase IN ('claimed','attached')",
                          (task['id'], run_id)).fetchone():
                raise DomainError('A previous app run is still unacknowledged. Stop or finish it in Codex, '
                                  'then have the dispatcher acknowledge its completion before retrying.')
            db.execute("INSERT INTO desktop_runs(run_id,phase) VALUES(?,'dispatched')", (run_id,))
        return True

    def claim(self, run_id):
        task, _ = self.active(run_id)
        with self.store.transaction() as db:
            self._require_active(db, run_id)
            entry = db.execute('SELECT * FROM desktop_runs WHERE run_id=?', (run_id,)).fetchone()
            if not entry:
                raise DomainError('Run was not dispatched', 404)
            if entry['phase'] != 'dispatched':
                return {'action': 'observe' if entry['thread_id'] else 'uncertain',
                        'session_id': entry['thread_id'], 'run_id': run_id}
            previous = db.execute("SELECT 1 FROM desktop_runs d JOIN runs r ON r.id=d.run_id "
                                  "WHERE r.task_id=? AND d.thread_id=? AND d.phase='complete'",
                                  (task['id'], task['session_id'])).fetchone()
            session = task['session_id'] if previous else None
            db.execute("UPDATE desktop_runs SET phase='claimed',thread_id=? WHERE run_id=?", (session, run_id))
        from .repositories import catalog
        return {'action': 'resume' if session else 'create', 'session_id': session,
                'run_id': run_id, 'task': task,
                'repositories': catalog(self.service.settings) if not task['repo_path'] else []}

    async def prepare(self, run_id, repo=''):
        task, run = self.active(run_id)
        if not task['repo_path']:
            from .repositories import catalog
            allowed = {r['path'] for r in await asyncio.to_thread(catalog, self.service.settings)}
            chosen = str(Path(repo).expanduser().resolve()) if repo else ''
            if chosen not in allowed:
                raise DomainError('Choose a repository from the supplied catalog, or request input.')
            with self.store.transaction() as db:
                current = db.execute('SELECT status,stop_requested FROM runs WHERE id=?', (run_id,)).fetchone()
                if current['status'] != 'running' or current['stop_requested']:
                    raise DomainError('Run cancelled before repository preparation')
                db.execute('UPDATE tasks SET repo_path=? WHERE id=?', (chosen, task['id']))
            task = self.service.get_task(task['id'])
        task, run = self.active(run_id)
        run['inputs'] = json.loads(run['inputs'])
        command = self.command()
        connector = (
            'Choose your working directory and any worktree using your normal workflow and '
            'repository instructions. ACP does not manage branches or workspaces.\n\n'
            f'ACP connection for this run: first run `{command} started {run_id}`. '
            'Your CODEX_THREAD_ID identifies this chat. If ACP rejects an inactive or '
            'mismatched run, stop. Use normal app approvals for sandbox/network errors.\n'
            f'Task commands: `{command} action {run_id} ACTION [OPTIONS]`\n'
            'Actions: get (current context/cancellation check); comment --text "progress"; '
            'input --text "blocking question"; review --text "result and verification" '
            '[--output-url PATH_OR_URL --kind text|file|url|pr]. '
            'If authorized to use subagents, report their observed state changes with '
            "`activity --text '{\"SUBAGENT_ID\":\"running\"}'` after starting/resuming them and "
            'after each wait/completion (use completed, interrupted, errored, or waiting_on_input as observed). '
            'Include nested subagents when visible via list_agents; report only this run’s descendants. '
            'Do not infer activity from commentary or create subagents just for tracking. '
            'Put evidence in ACP comment/review text, not only in the chat reply: '
            'screenshots as ![description](</absolute/path/screenshot.png>) and '
            'documents as [description](</absolute/path/report.md>). Use absolute local paths '
            'with angle brackets, or existing HTTPS links. For a primary file deliverable, '
            'use --output-url /absolute/path/report.md --kind file. ACP snapshots explicitly '
            'linked local files and uploads them to the linked Linear issue during comment sync. '
            'Files must exist and be readable when first submitted (20 MiB per file, '
            '50 MiB total per comment). Include only intended evidence, never credentials '
            'or unrelated files. Plain paths, relative links, and chat-only attachments '
            'are not uploaded. Report actual verification and missing evidence honestly. '
            'After input or review, end your turn for the user to respond.\n'
            'When a pull request is the intended deliverable, commit and push the changes to the '
            'repository remote, create or update the PR, and report its canonical URL with '
            '`review --text "result and verification" --output-url PR_URL --kind pr`. A local commit or a plan to open a PR '
            'does not complete that outcome. Follow repository instructions and normal app approvals.\n')
        return {'task_id': task['id'], 'repo_path': task['repo_path'],
                'prompt': build_prompt(task, run, connector), 'title': f"ACP: {task['title']}"}

    def bind(self, run_id, session):
        session = thread_id(session)
        with self.store.transaction() as db:
            entry = db.execute('SELECT * FROM desktop_runs WHERE run_id=?', (run_id,)).fetchone()
            if not entry or entry['phase'] not in {'claimed', 'attached'}:
                raise DomainError('Claim the dispatched run before attaching a chat')
            if entry['thread_id'] and entry['thread_id'] != session:
                raise DomainError('This run is already bound to another chat')
            db.execute("UPDATE desktop_runs SET phase='attached',thread_id=? WHERE run_id=?", (session, run_id))
        # Recovery-only binding records a created chat even if the run was revoked.
        # remember_session itself refuses revoked runs, so this cannot revive execution.
        self.service.remember_session(run_id, session)
        return {'session_id': session}

    def started(self, run_id, session):
        self.active(run_id)
        result = self.bind(run_id, session)
        if not self.store.one('SELECT started_at FROM runs WHERE id=?', (run_id,))['started_at']:
            self.service.mark_started(run_id)
        return result

    def action(self, run_id, session, action, text='', output_url='', kind='text'):
        task, _ = self.active(run_id)
        entry = self.store.one('SELECT * FROM desktop_runs WHERE run_id=?', (run_id,))
        if not entry or entry['thread_id'] != thread_id(session) or entry['phase'] != 'attached':
            raise DomainError('Chat is not bound to this run')
        if action == 'activity':
            from .activity import Activity, RUNNING, WAITING, ENDED
            try:
                changes = json.loads(text)
            except (ValueError, TypeError):
                raise DomainError('Activity must be a JSON object mapping subagent IDs to observed states.', 422)
            if (not isinstance(changes, dict) or len(changes) > 256 or
                    any(not key or len(key) > 256 or not isinstance(value, str) or
                        value not in RUNNING | WAITING | ENDED for key, value in changes.items())):
                raise DomainError('Use subagent IDs and running, waiting_on_input, or terminal states.', 422)
            if not Activity(self.store).report(run_id, changes):
                raise DomainError('This run is no longer doing work; stop reporting activity.')
            return {'recorded': True}
        if action == 'get':
            return task
        if action == 'comment':
            return self.service.add_comment(task['id'], text, run_id=run_id)
        if action == 'input':
            return self.service.request_input(task['id'], text, run_id)
        if action == 'review':
            return self.service.submit_for_review(task['id'], text, output_url, kind, run_id)
        raise DomainError('Unknown desktop task action', 422)

    def steering(self, run_id):
        """Claim one comment for the dispatcher without starting a second run."""
        with self.store.transaction() as db:
            task, _ = self._require_active(db, run_id)
            entry = db.execute('SELECT * FROM desktop_runs WHERE run_id=?', (run_id,)).fetchone()
            if not entry or entry['phase'] != 'attached' or not entry['thread_id']:
                raise DomainError('Attach the task chat before checking steering')
            db.execute("DELETE FROM desktop_inputs WHERE run_id=? AND status='pending' "
                       "AND comment_id IN (SELECT id FROM comments WHERE deleted=1)", (run_id,))
            pending = db.execute(
                "SELECT i.*,c.body,c.author FROM desktop_inputs i JOIN comments c ON c.id=i.comment_id "
                "WHERE i.run_id=? AND i.status!='delivered' ORDER BY c.created_at,c.rowid LIMIT 1",
                (run_id,)).fetchone()
            if not pending:
                return {'action': 'wait'}
            if task['state'] != 'in_progress':
                raise DomainError('Task is no longer accepting steering; reconcile its pending comments')
            action = 'uncertain' if pending['status'] == 'claimed' else 'send'
            db.execute("UPDATE desktop_inputs SET status='claimed' WHERE comment_id=?", (pending['comment_id'],))
            prompt = (
                f"New human comment on this ACP task (comment {pending['comment_id']}, run {run_id}). "
                'Incorporate this feedback into the current task now. Keep the same ACP run and chat. '
                f'First check `{self.command()} action {run_id} get`; if revoked, stop. '
                'Do not treat this as a new task or create another chat. '
                'Before requesting input or submitting for review, allow the dispatcher to confirm delivery.\n\n'
                f"{pending['author']}:\n{pending['body']}")
            return {'action': action, 'session_id': entry['thread_id'],
                    'comment_id': pending['comment_id'], 'prompt': prompt}

    def steered(self, run_id, session, comment_id):
        session = thread_id(session)
        with self.store.transaction() as db:
            task, _ = self._require_active(db, run_id)
            entry = db.execute('SELECT * FROM desktop_runs WHERE run_id=?', (run_id,)).fetchone()
            delivery = db.execute('SELECT * FROM desktop_inputs WHERE run_id=? AND comment_id=?',
                                  (run_id, comment_id)).fetchone()
            if (not entry or entry['phase'] != 'attached' or entry['thread_id'] != session or
                    not delivery or delivery['status'] == 'pending'):
                raise DomainError('Delivery does not match a claimed comment and bound chat')
            if delivery['status'] != 'delivered':
                db.execute("UPDATE desktop_inputs SET status='delivered' WHERE comment_id=?", (comment_id,))
                self.service._event(db, 'run.input_delivered', task['id'],
                                    {'run_id': run_id, 'comment_id': comment_id})
        return {'delivered': True}

    def complete(self, run_id, session, error=''):
        # Completion may acknowledge an already revoked run, but never revive it.
        session = thread_id(session) if session else None
        with self.store.transaction() as db:
            entry = db.execute('SELECT * FROM desktop_runs WHERE run_id=?', (run_id,)).fetchone()
            if entry and error and not session:
                session = entry['thread_id']
            if not entry or entry['thread_id'] != session:
                raise DomainError('Completion does not match the dispatched chat')
            if not session and not error:
                raise DomainError('An undispatched run requires a failure explanation')
            run = db.execute('SELECT * FROM runs WHERE id=?', (run_id,)).fetchone()
            if (not error and run['status'] == 'running' and not run['stop_requested'] and db.execute(
                    "SELECT 1 FROM desktop_inputs i JOIN comments c ON c.id=i.comment_id "
                    "WHERE i.run_id=? AND (i.status='claimed' OR (i.status='pending' AND c.deleted=0))",
                    (run_id,)).fetchone()):
                raise DomainError('Reconcile pending comments before acknowledging this run')
            db.execute("UPDATE desktop_runs SET phase='complete' WHERE run_id=?", (run_id,))
        self.service.finish_run(run_id, 'failed' if error else 'succeeded', error or None)
        return {'acknowledged': True}

    def dispatcher_prompt(self, run_id):
        cmd = self.command()
        return (f'ACP dispatched run {run_id}. The user authorizes creating or continuing its Codex app chat. '
                f'First run {cmd} claim {run_id}. If revoked, stop. For sandbox/network permission errors, '
                'request normal execution approval and retry; do not treat a transport error as a revoked run. '
                'Use the returned action:\n'
                '- uncertain: do not create anything; report that a previous dispatch needs reconciliation.\n'
                '- observe: only observe the supplied chat, never send its prompt again.\n'
                '- create/resume: choose the repository from task.repo_path or the supplied catalog, '
                f'then run {cmd} prepare {run_id} [--repo ABSOLUTE_PATH] to get the exact task prompt. '
                'If ambiguous, report an error with acknowledge rather than guessing. '
                'For create, call list_projects and use a matching local project with environment local; '
                'otherwise use projectless. Call create_thread exactly once with the returned title and prompt. '
                'The task agent manages its own working directory and any worktree using normal app tools. '
                'For resume, send the exact prompt to the supplied session using send_message_to_thread. '
                f'After creation or continuation, run {cmd} bind {run_id} --thread THREAD_ID.\n'
                f'While observing the task, check `{cmd} steering {run_id}` before each wait. '
                'A send action is a new human comment: the user authorizes forwarding its exact prompt '
                'to its session_id using send_message_to_thread immediately, even while the chat is active. '
                f'After a successful send, run `{cmd} steered {run_id} --thread THREAD_ID --comment-id COMMENT_ID`. '
                'Drain send actions before waiting. An uncertain action means a previous send may have succeeded: '
                'inspect the target chat for that comment ID and acknowledge only confirmed delivery; '
                'never blindly resend. If delivery cannot be established, report the problem and reconcile it '
                'before acknowledging the run. A wait action means no comments are pending. '
                'Use wait_threads with timeoutMs 10000 and its latest afterCursor, then check steering again; '
                'do not wait for task completion before delivering new comments. '
                'If steering is rejected because the run was revoked, stop forwarding comments but keep '
                'observing the task chat and acknowledge its actual completion as usual. '
                'Do not interpret commentary as completion. '
                'If it needs user approval, leave the request for the user and keep waiting. '
                f'After actual completion run {cmd} acknowledge {run_id} --thread THREAD_ID '
                '[--error "Actual failure explanation"]. If preparation failed before sending a prompt, acknowledge with --error only. '
                'Acknowledging a finished turn only settles its run record; it never marks the task Done. '
                'A chat that asked for input or submitted for review has finished its turn and must be acknowledged; '
                'the task remains Waiting on Input or Ready for Review until the user acts. '
                'This acknowledgement is part of the authorized workflow and needs no additional confirmation. '
                'Never acknowledge while the chat is still running. Do not do task work yourself. '
                'If interrupted before binding, reconcile the chat through list_threads before another creation.')


class DesktopRunner:
    async def run(self, service, task, run):
        bridge = DesktopBridge(service)
        connection = bridge.status()
        if not connection['connected']:
            if task['session_id'] and service.store.one(
                    'SELECT 1 FROM desktop_runs WHERE thread_id=?', (task['session_id'],)):
                raise DomainError('Reconnect the Codex dispatcher to continue this app chat, or reset its session first.')
            return await CLIRunner().run(service, task, run)
        bridge.dispatch(run['id'])
        env = {k: v for k, v in os.environ.items() if not k.startswith('LINEAR_')
               and k not in {'GITHUB_WEBHOOK_SECRET', 'GITHUB_TOKEN'}}
        process = await asyncio.create_subprocess_exec(
            service.settings.codex_command, 'queue', '--thread', connection['thread_id'],
            '--message', bridge.dispatcher_prompt(run['id']), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), 30)
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()
        log = service.settings.data_dir / 'logs' / f"{run['id']}.jsonl"
        log.parent.mkdir(exist_ok=True)
        log.write_text(json.dumps({'desktop_dispatch': stdout.decode(errors='replace'),
                                  'error': stderr.decode(errors='replace')}) + '\n')
        if process.returncode:
            raise RuntimeError('Codex queue failed. Check Codex installation and the run log.')
        queued_at = time.monotonic()
        deadline = queued_at + service.settings.run_timeout_seconds
        while True:
            current = service.store.one('SELECT * FROM runs WHERE id=?', (run['id'],))
            if current['status'] != 'running' or current['stop_requested']:
                return
            now = time.monotonic()
            if not current['started_at'] and now - queued_at >= min(120, service.settings.run_timeout_seconds):
                raise TimeoutError('The Codex app chat did not start within two minutes. Open Codex and inspect '
                                   'the dispatcher connection and any approval requests before retrying.')
            if now >= deadline:
                raise TimeoutError('No completed app result before the run timeout. Open Codex and inspect '
                                   'the dispatcher/task chat. Stop any active turn there before retrying.')
            await asyncio.sleep(service.settings.poll_seconds)
