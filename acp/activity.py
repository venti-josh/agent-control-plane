"""Observed ACP agent concurrency. No transcript text or prompts are stored here."""
from __future__ import annotations

import time

from .store import Store

RUNNING = {'pending_init', 'running'}
WAITING = {'waiting_on_input'}
ENDED = {'completed', 'interrupted', 'errored', 'shutdown', 'not_found', 'failed', 'stopped', 'killed'}


class Activity:
    def __init__(self, store: Store):
        self.store = store

    def observe(self, run_id: str, provider: str, event: dict) -> None:
        changes = {}
        if provider == 'codex' and event.get('type') in {'item.started', 'item.updated', 'item.completed'}:
            item = event.get('item')
            if not isinstance(item, dict) or item.get('type') != 'collab_tool_call':
                return
            states = item.get('agents_states')
            if not isinstance(states, dict):
                return
            for agent_id, state in states.items():
                if isinstance(state, dict) and isinstance(state.get('status'), str):
                    changes[agent_id] = state['status']
        elif provider == 'claude' and event.get('type') == 'system':
            subtype, agent_id = event.get('subtype'), event.get('task_id')
            if isinstance(agent_id, str) and agent_id:
                if subtype == 'task_started' and event.get('task_type') == 'local_agent':
                    changes[agent_id] = 'running'
                elif subtype == 'task_notification' and event.get('status') in ENDED:
                    changes[agent_id] = event['status']
                elif subtype == 'task_updated' and isinstance(event.get('patch'), dict):
                    status = event['patch'].get('status')
                    if isinstance(status, str) and status in ENDED:
                        changes[agent_id] = status
        if changes:
            self.report(run_id, changes)

    def report(self, run_id: str, changes: dict) -> bool:
        """Apply observed state changes; duplicate observations do not reset intervals."""
        with self.store.transaction() as db:
            parent = db.execute("SELECT r.session_id FROM runs r JOIN tasks t ON t.id=r.task_id "
                                "WHERE r.id=? AND r.status='running' AND r.started_at IS NOT NULL "
                                "AND r.stop_requested=0 AND r.activity_finished_at IS NULL "
                                "AND t.state='in_progress' AND t.hidden=0", (run_id,)).fetchone()
            if not parent:
                return False
            now = time.time()
            for agent_id, status in changes.items():
                if not isinstance(agent_id, str) or not agent_id or agent_id == parent['session_id']:
                    continue
                if not isinstance(status, str) or status not in RUNNING | WAITING | ENDED:
                    continue
                state = 'running' if status in RUNNING else 'waiting_on_input' if status in WAITING else None
                current = db.execute('SELECT id,state FROM subagent_activity '
                                     'WHERE run_id=? AND agent_id=? AND finished_at IS NULL',
                                     (run_id, agent_id)).fetchone()
                if current and current['state'] == state:
                    continue
                if current:
                    db.execute('UPDATE subagent_activity SET finished_at=? WHERE id=?', (now, current['id']))
                if state:
                    db.execute('INSERT INTO subagent_activity(run_id,agent_id,state,started_at) VALUES(?,?,?,?)',
                               (run_id, agent_id, state, now))
        return True

    def summary(self) -> dict:
        now = time.time()
        with self.store.connect() as db:
            db.execute('BEGIN')
            running = db.execute("SELECT count(*) FROM runs WHERE status='running' "
                                 "AND started_at IS NOT NULL AND activity_finished_at IS NULL").fetchone()[0]
            children = dict(db.execute('SELECT a.state,count(*) FROM subagent_activity a '
                                       'JOIN runs r ON r.id=a.run_id WHERE a.finished_at IS NULL '
                                       "AND r.status='running' AND r.activity_finished_at IS NULL "
                                       'GROUP BY a.state').fetchall())
            waiting = db.execute("SELECT count(*) FROM tasks t WHERE state='waiting_on_input' AND hidden=0 "
                                 "AND NOT EXISTS(SELECT 1 FROM runs r WHERE r.task_id=t.id "
                                 "AND r.status='queued')").fetchone()[0]
            averages = {}
            for label, seconds in [('30m', 1800), ('1h', 3600), ('3h', 10800)]:
                total = db.execute('''
                    SELECT coalesce(sum(max(0, min(coalesce(finished_at, ?), ?) - max(started_at, ?))), 0)
                    FROM (
                        SELECT started_at, coalesce(activity_finished_at, finished_at) AS finished_at
                        FROM runs WHERE started_at IS NOT NULL
                        UNION ALL
                        SELECT a.started_at, min(coalesce(a.finished_at, ?),
                                                coalesce(r.activity_finished_at, r.finished_at, ?))
                        FROM subagent_activity a JOIN runs r ON r.id=a.run_id WHERE a.state='running'
                    ) WHERE started_at < ? AND coalesce(finished_at, ?) > ?
                ''', (now, now, now - seconds, now, now, now, now, now - seconds)).fetchone()[0]
                averages[label] = total / seconds
        subagents = children.get('running', 0)
        return {'running': running + subagents, 'subagents': subagents,
                'waiting_on_input': waiting + children.get('waiting_on_input', 0), 'averages': averages}
