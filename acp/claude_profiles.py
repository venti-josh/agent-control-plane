"""Human profile selection and aliases for the standalone migration CLI."""
from __future__ import annotations

import fcntl
import os
import re
import shlex
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from .claude_migration import atomic_write
from .claude_sessions import encode, object_json, safe_path

ALIAS = re.compile(r'[a-zA-Z][a-zA-Z0-9_-]{0,39}\Z')


def say(message):
    print(message, file=sys.stderr)


def ask(message):
    print(message, end=' ', file=sys.stderr, flush=True)
    return input().strip()


def display(value):
    # Session titles and local metadata are untrusted terminal text.
    return ''.join(c if c.isprintable() else ' ' for c in str(value))[:160]


def timestamp(value):
    try:
        # Desktop records use epoch milliseconds.
        return datetime.fromtimestamp(value / 1000, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')
    except (ValueError, OverflowError, OSError):
        return 'date unavailable'


class Profiles:
    def __init__(self, store, home):
        self.store = store
        self.home = home

    def load(self):
        path = safe_path(self.home, 'claude-profiles.json')
        data = object_json(path.read_bytes()) if path.exists() else {'version': 1, 'stores': {}}
        if data.get('version') != 1 or not isinstance(data.get('stores'), dict):
            raise ValueError('Invalid Claude profile aliases file')
        for root, aliases in data['stores'].items():
            if not isinstance(root, str) or not isinstance(aliases, dict) or not all(
                    isinstance(name, str) and ALIAS.fullmatch(name) and isinstance(target, str)
                    for name, target in aliases.items()):
                raise ValueError('Invalid Claude profile aliases file')
        return data

    def aliases(self):
        return self.load()['stores'].get(str(self.store.root), {})

    def resolve(self, selector):
        if '/' in selector:
            return self.store.select(selector)
        target = self.aliases().get(selector)
        if target is None:
            raise ValueError(f'Unknown profile alias {selector!r}; run `acp claude profiles` or use ACCOUNT/ORG')
        return self.store.select(target)

    def validate_alias(self, name, target, aliases):
        if not ALIAS.fullmatch(name):
            raise ValueError('Alias must start with a letter and contain 1–40 letters, digits, underscores or hyphens')
        self.store.select(target)
        if name in aliases and aliases[name] != target:
            raise ValueError(f'Alias {name!r} already names another profile; choose a different name')

    def save(self, additions):
        self.home.mkdir(parents=True, mode=0o700, exist_ok=True)
        path = safe_path(self.home, 'claude-profiles.lock')
        fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            data = self.load()
            aliases = data['stores'].setdefault(str(self.store.root), {})
            for name, target in additions.items():
                self.validate_alias(name, target, aliases)
            aliases.update(additions)
            atomic_write(safe_path(self.home, 'claude-profiles.json'), encode(data))
        finally:
            os.close(fd)

    def list(self):
        aliases = self.aliases()
        # CLI metadata supplies labels only. It never establishes the Desktop login.
        config_dir = os.getenv('CLAUDE_CONFIG_DIR')
        metadata_path = Path(config_dir).expanduser() / '.claude.json' if config_dir else Path.home() / '.claude.json'
        try:
            metadata = object_json(metadata_path.read_bytes()).get('oauthAccount', {})
            if not isinstance(metadata, dict):
                metadata = {}
        except (OSError, ValueError, UnicodeError):
            metadata = {}
        principal = f'{metadata.get("accountUuid")}/{metadata.get("organizationUuid")}'
        result = []
        self.store.observed = {}
        for account in self.store.accounts():
            unsupported = []
            records = self.store.records(account, unsupported)
            recent = sorted((data for data, _ in records.values()), key=lambda d: d['lastActivityAt'], reverse=True)[:3]
            matching = account == principal
            result.append(dict(account=account, aliases=sorted(k for k, v in aliases.items() if v == account),
                               email=metadata.get('emailAddress') if matching and isinstance(metadata.get('emailAddress'), str) else None,
                               organization=metadata.get('organizationName') if matching and isinstance(metadata.get('organizationName'), str) else None,
                               label_source='CLI metadata (not Desktop login)' if matching else None,
                               session_count=len(records), unsupported_count=len(unsupported),
                               recent_sessions=[{'title': d.get('title', d['sessionId']),
                                                 'last_activity': timestamp(d['lastActivityAt'])} for d in recent]))
        return result

    def choose(self, role, exclude=None):
        while True:
            profiles = [p for p in self.list() if p['account'] != exclude]
            say(f'\nChoose {role}:')
            for number, profile in enumerate(profiles, 1):
                label = ', '.join(profile['aliases']) or profile['email'] or 'Unnamed profile'
                org = f' — {display(profile["organization"])}' if profile['organization'] else ''
                say(f'  {number}. {display(label)}{org} ({profile["session_count"]} sessions)')
                say(f'     {profile["account"]}')
                if profile['label_source']:
                    say(f'     {profile["label_source"]}')
                for session in profile['recent_sessions']:
                    say(f'     {session["last_activity"]} — {display(session["title"])}')
            say('  r. Rescan after switching accounts / creating a recognizable Code session')
            say('  n. Sign in to another Desktop account and watch for its new folder')
            say('  q. Cancel')
            choice = ask('Number, alias, r, n, or q:')
            if choice.lower() == 'q':
                raise KeyboardInterrupt
            if choice.lower() == 'r':
                continue
            if choice.lower() == 'n':
                before = set(self.store.accounts())
                say(f'Sign in to the {role} account/org in Desktop and open the Code tab. '
                    'Watching for a new profile; Ctrl-C cancels.')
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline and not (set(self.store.accounts()) - before):
                    time.sleep(1)
                if not (set(self.store.accounts()) - before):
                    say('No new folder yet. If it already exists, use its session titles to select it.')
                continue
            if choice.isascii() and choice.isdecimal() and 1 <= int(choice) <= len(profiles):
                return profiles[int(choice) - 1]['account']
            if choice in self.aliases():
                selected = self.resolve(choice)
                if selected != exclude:
                    return selected
            say('Choose one of the listed profiles; source and destination must differ.')


def preview(report):
    say(f'\nSource:      {report["source"]}\nDestination: {report["destination"]}')
    say(f'Warning: {report["unread_state"]["reason"]}')
    say(f'Copy/update {len(report["restored"])} sessions; '
        f'preserve {len(report["preserved_as_newer"])} newer sessions; '
        f'{len(report["already_present"])} already present; {len(report["changes"])} files to change.')
    say(f'Additional issues (skips, conflicts, or warnings): {len(report["unavailable"])}')
    for issue in report['unavailable']:
        say(f'  {display(issue.get("session", "Metadata"))}: {display(issue["reason"])}')
    for path in report['changes']:
        say(f'  Change: {path}')


def migrate(profiles, destination=None, source=None, dry_run=False, yes=False):
    store = profiles.store
    interactive = sys.stdin.isatty() and not yes
    if (yes and (not destination or not source)) or (not interactive and not dry_run and not yes):
        raise ValueError('Unattended migration requires --from PROFILE --to PROFILE --yes; use --dry-run to preview')
    if not interactive and not destination:
        raise ValueError('Supply --from PROFILE --to PROFILE with --dry-run or --yes, or run the wizard in a terminal')
    pending_aliases = {}
    try:
        wizard = interactive and (not destination or not source)
        if wizard:
            say('Select the account/org that should RECEIVE the saved Code sessions first. '
                'Desktop login cannot be reliably determined from local session folders.')
        target = profiles.resolve(destination) if destination else profiles.choose('destination')
        origin = profiles.resolve(source) if source else (profiles.choose('source', exclude=target) if interactive else None)
        report, changes = store.plan(target, origin)
        preview(report)
        if dry_run:
            return {**report, 'dry_run': True, 'disk_verified': False}
        if interactive:
            if wizard:
                for role, account in [('source', report['source']), ('destination', target)]:
                    if account in profiles.aliases().values():
                        continue
                    while True:
                        name = ask(f'Optional name for {role} (e.g. {"personal" if role == "source" else "work"}; Enter skips):')
                        if not name:
                            break
                        try:
                            profiles.validate_alias(name, account, {**profiles.aliases(), **pending_aliases})
                        except ValueError as exc:
                            say(str(exc))
                            continue
                        pending_aliases[name] = account
                        break
            say('Quit Claude Desktop and stop Claude CLI/background sessions before continuing. '
                'Source sessions stay intact; changed destination files will be backed up.')
            if ask('Type migrate to apply this preview (anything else cancels):') != 'migrate':
                return {'cancelled': True, 'dry_run': False, 'disk_verified': False}
    except (EOFError, KeyboardInterrupt):
        say('\nCancelled. No migration or alias writes were made.')
        return {'cancelled': True, 'dry_run': dry_run, 'disk_verified': False}
    result = store.migrate(target, report['source'], expected_plan=(report, changes))
    # Alias persistence is secondary: never hide a successful migration/undo ID if it fails.
    if pending_aliases:
        try:
            profiles.save(pending_aliases)
        except (OSError, ValueError) as exc:
            result['alias_warning'] = f'Migration completed but aliases were not saved: {exc}'
            say(result['alias_warning'])
    if result.get('migration_id'):
        undo = shlex.join(['acp', 'claude', 'rollback', result['migration_id'],
                          '--sessions-dir', str(store.root), '--projects-dir', str(store.projects),
                          '--backup-dir', str(store.backups)])
        result['undo_command'] = undo
        say(f'Backup: {result["backup"]}\nTo undo with Claude closed:\n  {undo}')
    say('Reopen Desktop, sign in to the destination and check your sessions, then quit and reopen again. '
        'Disk verification does not prove Desktop visibility.')
    return result
