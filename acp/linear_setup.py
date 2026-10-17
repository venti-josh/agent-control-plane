"""Guided Linear configuration shared by the browser and agent-friendly CLI."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import tempfile
from urllib.parse import urlparse
from uuid import uuid4

import httpx

from .config import LABELS, STATES, Settings
from .service import DomainError

PAGE = 'pageInfo { hasNextPage endCursor }'
TYPES = dict(zip(STATES, ('backlog', 'unstarted', 'started', 'started', 'started', 'started', 'completed')))
ALIASES = {'planning': ('planning', 'backlog'), 'todo': ('todo', 'to do'),
           'in_progress': ('in progress',), 'waiting_on_input': ('waiting on input', 'blocked'),
           'ready_for_review': ('ready for review', 'in review'), 'deploy': ('deploy',), 'done': ('done',)}


class LinearSetup:
    def __init__(self, service):
        self.service, self.settings, self.store = service, service.settings, service.store
        self.lock = asyncio.Lock()

    def key(self, supplied=''):
        key = supplied.strip() or self.settings.linear_api_key
        if not key:
            # The user may have just pasted a key into config.env while ACP was running.
            local = Settings.from_env()
            if local.data_dir == self.settings.data_dir:
                key = local.linear_api_key
        if not key:
            raise DomainError('Enter a Linear API key, or save LINEAR_API_KEY in config.env.', 422)
        return key

    async def query(self, key, query, variables=None):
        try:
            async with httpx.AsyncClient(timeout=20, trust_env=False) as client:
                response = await client.post('https://api.linear.app/graphql',
                                             headers={'Authorization': key},
                                             json={'query': query, 'variables': variables or {}})
            if response.status_code in (401, 403):
                raise DomainError('Linear rejected access. Check the API key and team permissions.', 422)
            response.raise_for_status()
            result = response.json()
            if result.get('errors'):
                # Provider messages may echo inputs, including credentials. Never forward them.
                raise DomainError('Linear rejected the operation. Check key permissions; webhook setup requires a workspace admin.', 422)
            data = result['data']
            if any(isinstance(v, dict) and v.get('success') is False for v in data.values()):
                raise DomainError('Linear did not apply the change. Refresh and retry.', 502)
            return data
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            raise DomainError('Could not reach Linear or read its response. Retry after checking connectivity.', 502) from None

    async def discover(self, api_key=''):
        key, teams, cursor = self.key(api_key), [], None
        while True:
            data = await self.query(key, 'query ACPDiscover($after:String){organization{id name} viewer{id name} '
                                    'teams(first:100,after:$after){nodes{id name key} ' + PAGE + '}}', {'after': cursor})
            teams.extend(data['teams']['nodes'])
            page = data['teams']['pageInfo']
            if not page['hasNextPage']:
                break
            cursor = page['endCursor']
        for team in teams:
            states, cursor = [], None
            while True:
                result = await self.query(key, 'query ACPStates($id:String!,$after:String){team(id:$id){id '
                                          'states(first:100,after:$after){nodes{id name type} ' + PAGE + '}}}',
                                          {'id': team['id'], 'after': cursor})
                connection = result['team']['states']
                states.extend(connection['nodes'])
                if not connection['pageInfo']['hasNextPage']:
                    break
                cursor = connection['pageInfo']['endCursor']
            suggested = {}
            for lane in STATES:
                matches = [s for s in states if s['name'].strip().casefold() in ALIASES[lane]]
                if len(matches) == 1:
                    suggested[lane] = matches[0]['id']
            if team['id'] == self.settings.linear_team_id:
                valid = {s['id'] for s in states}
                suggested.update({k: v for k, v in self.settings.linear_states.items() if v in valid})
            team.update(states=states, suggested_states=suggested,
                        missing=[lane for lane in STATES if lane not in suggested])
        return {'workspace': data['organization'], 'user': data['viewer'], 'teams': teams}

    def saved(self):
        return self.settings.load_linear()

    def status(self):
        saved = self.saved()
        received = self.store.one("SELECT max(created_at) at FROM receipts WHERE source='linear'")
        failures = self.store.one("SELECT count(*) n FROM outbox WHERE kind LIKE 'linear.%' AND status='failed'")
        sync_error = self.store.one("SELECT value FROM metadata WHERE key='linear_sync_error'")
        last_sync = self.store.one("SELECT value FROM metadata WHERE key='linear_last_sync'")
        return {'configured': self.settings.linear_enabled,
                'label_sync': self.settings.linear_label_sync, 'provider': self.settings.linear_provider,
                'sync_error': sync_error['value'] if sync_error else None,
                'last_sync_at': float(last_sync['value']) if last_sync else None,
                'workspace_id': self.settings.linear_workspace_id,
                'workspace_name': saved.get('workspace_name', ''),
                'team_id': self.settings.linear_team_id, 'team_name': saved.get('team_name', ''),
                'user_id': self.settings.linear_user_id, 'user_name': saved.get('user_name', ''),
                'states': self.settings.linear_states, 'webhook_url': saved.get('webhook_url', ''),
                'webhook_registered': saved.get('webhook_registered', False),
                'webhook_secret_set': bool(self.settings.linear_webhook_secret),
                'last_webhook_at': received['at'], 'failed_deliveries': failures['n']}

    def persist(self, data):
        path = self.settings.data_dir / 'linear.json'
        fd, name = tempfile.mkstemp(prefix='.linear-', dir=path.parent)
        try:
            with os.fdopen(fd, 'w') as file:
                json.dump(data, file)
                file.flush()
                os.fsync(file.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        self.settings.load_linear()

    async def ensure_label(self, key, team_id):
        cursor = None
        while True:
            data = await self.query(key, 'query ACPLabels($after:String){issueLabels(first:100,after:$after)'
                                    '{nodes{id name team{id}} ' + PAGE + '}}', {'after': cursor})
            for label in data['issueLabels']['nodes']:
                if label['name'].casefold() == 'acp' and (not label.get('team') or label['team']['id'] == team_id):
                    return label['id']
            if not data['issueLabels']['pageInfo']['hasNextPage']:
                break
            cursor = data['issueLabels']['pageInfo']['endCursor']
        result = await self.query(key, 'mutation($input:IssueLabelCreateInput!){issueLabelCreate(input:$input)'
                                  '{success issueLabel{id}}}',
                                  {'input': {'name': 'acp', 'teamId': team_id, 'color': '#64748b'}})
        return result['issueLabelCreate']['issueLabel']['id']

    async def configure(self, api_key='', team_id='', states=None, create_missing=False, provider='codex'):
        async with self.lock:
            discovery = await self.discover(api_key)
            workspace = discovery['workspace']
            team = next((t for t in discovery['teams'] if t['id'] == team_id), None)
            if not team:
                raise DomainError('Choose a team from the discovered teams.', 422)
            pinned = self.store.one("SELECT value FROM metadata WHERE key='linear_workspace'")
            if pinned and pinned['value'] != workspace['id']:
                raise DomainError('This board belongs to another Linear workspace. Use a separate ACP data directory.')
            owner = self.store.one("SELECT value FROM metadata WHERE key='linear_user'")
            if owner and owner['value'] != discovery['user']['id']:
                raise DomainError('This board belongs to another Linear user. Use a separate ACP data directory.')
            mapping = {**team['suggested_states'], **(states or {})}
            valid = {s['id'] for s in team['states']}
            if set(mapping) - set(STATES) or any(v and v not in valid for v in mapping.values()):
                raise DomainError('Every mapped status must belong to the selected team.', 422)
            selected = [v for v in mapping.values() if v]
            if len(selected) != len(set(selected)):
                raise DomainError('Choose a different Linear status for each ACP lane.', 422)
            missing = [lane for lane in STATES if not mapping.get(lane)]
            if missing and not create_missing:
                raise DomainError('Map the missing lanes or enable Create missing statuses: ' + ', '.join(missing), 422)
            linked = self.store.one('SELECT count(*) n FROM tasks WHERE linear_id IS NOT NULL')['n']
            if linked and (team_id != self.settings.linear_team_id or mapping != self.settings.linear_states):
                raise DomainError('This board has imported issues. Keep its team and status mapping, or use a separate ACP data directory.')
            for lane in missing:
                result = await self.query(self.key(api_key),
                    'mutation($input:WorkflowStateCreateInput!){workflowStateCreate(input:$input){success workflowState{id name type}}}',
                    {'input': {'teamId': team_id, 'name': LABELS[STATES.index(lane)],
                               'type': TYPES[lane], 'color': '#64748b'}})
                mapping[lane] = result['workflowStateCreate']['workflowState']['id']
            await self.ensure_label(self.key(api_key), team_id)
            saved = self.saved()
            # Webhooks are team scoped. Never silently reuse one for another team.
            if team_id != self.settings.linear_team_id:
                saved = {}
            saved.update(linear_api_key=self.key(api_key), linear_workspace_id=workspace['id'],
                         workspace_name=workspace['name'], linear_team_id=team_id, team_name=team['name'],
                         linear_states=mapping, linear_webhook_secret=saved.get('linear_webhook_secret', ''),
                         linear_label_sync=True, linear_provider=provider,
                         linear_user_id=discovery['user']['id'], user_name=discovery['user']['name'])
            with self.store.transaction() as db:
                db.execute("INSERT OR IGNORE INTO metadata VALUES('linear_workspace',?)", (workspace['id'],))
                db.execute("INSERT OR IGNORE INTO metadata VALUES('linear_user',?)", (discovery['user']['id'],))
                self.persist(saved)
            return self.status()

    async def webhook(self, url):
        async with self.lock:
            if not self.settings.linear_enabled:
                raise DomainError('Connect a Linear team first.', 422)
            parsed = urlparse(url)
            if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment or parsed.path != '/hooks/linear'
                    or parsed.hostname in ('localhost', '127.0.0.1', '::1')):
                raise DomainError('Use a public HTTPS forwarding URL ending in /hooks/linear, forwarding to port 8766.', 422)
            saved = self.saved()
            if not saved:
                raise DomainError('Save your team through setup before registering a webhook.', 422)
            # Persist the ID and secret BEFORE the remote call, so retries recover uncertain success.
            saved.update(webhook_id=saved.get('webhook_id') or str(uuid4()),
                         linear_webhook_secret=saved.get('linear_webhook_secret') or secrets.token_hex(32))
            self.persist(saved)
            cursor, existing = None, None
            while True:
                data = await self.query(self.key(), 'query ACPWebhooks($after:String){'
                                        'webhooks(first:100,after:$after){nodes{id url enabled team{id}} ' + PAGE + '}}',
                                        {'after': cursor})
                existing = next((h for h in data['webhooks']['nodes'] if h['id'] == saved['webhook_id']), None)
                if existing or not data['webhooks']['pageInfo']['hasNextPage']:
                    break
                cursor = data['webhooks']['pageInfo']['endCursor']
            if existing:
                if existing['url'] != url:
                    raise DomainError('Remove the existing ACP webhook in Linear before registering a different URL.')
                if not existing['enabled'] or existing['url'] != url or (existing.get('team') or {}).get('id') != self.settings.linear_team_id:
                    raise DomainError('The registered webhook was changed or disabled in Linear. Restore its URL, team and enabled state.')
            else:
                saved.update(webhook_url=url, webhook_registered=False)
                self.persist(saved)
                await self.query(self.key(), 'mutation($input:WebhookCreateInput!){webhookCreate(input:$input){success webhook{id}}}',
                                 {'input': {'id': saved['webhook_id'], 'url': url, 'teamId': self.settings.linear_team_id,
                                            'resourceTypes': ['Issue', 'Comment'], 'enabled': True,
                                            'label': 'ACP', 'secret': saved['linear_webhook_secret']}})
            saved.update(webhook_url=url, webhook_registered=True)
            self.persist(saved)
            return self.status()
