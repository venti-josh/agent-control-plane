"""User-facing migration selection uses isolated session stores and ACP settings."""
import json
import sys

import pytest

from acp import cli
from test_claude_sessions import record, store, tree, write  # reuse the migration fixtures


@pytest.fixture
def run_cli(store, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv('ACP_HOME', str(tmp_path / 'acp-home'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'cli-config'))

    def run(command, *arguments, answers=(), tty=True):
        replies = iter(answers)
        monkeypatch.setattr(sys.stdin, 'isatty', lambda: tty)
        monkeypatch.setattr('builtins.input', lambda: next(replies))
        monkeypatch.setattr(sys, 'argv', ['acp', 'claude', command, *arguments,
                            '--sessions-dir', str(store.root), '--projects-dir', str(store.projects),
                            '--backup-dir', str(store.backups)])
        code = 0
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
        output = capsys.readouterr()
        return code, output.out, output.err
    return run


def test_wizard_selects_by_number_previews_and_cancels_without_writes(store, run_cli):
    record(store, title='Recognizable source session', lastActivityAt=1700000000000)
    before = tree(store.root.parent.parent)
    code, out, err = run_cli('migrate', answers=['2', '1', '', '', 'no'])
    assert code == 0
    assert json.loads(out)['cancelled'] is True
    assert 'Recognizable source session' in err and '2023-11-14' in err
    assert 'Copy/update 1 sessions' in err and 'Source:' in err and 'Destination:' in err
    assert tree(store.root.parent.parent) == before


def test_wizard_applies_confirmed_direction_and_saves_aliases(store, run_cli, tmp_path):
    original = record(store, title='Session to move')
    code, out, err = run_cli('migrate', answers=['2', '1', 'personal', 'work', 'migrate'])
    assert code == 0, err
    result = json.loads(out)
    assert (store.root / 'account-b/org-b' / original.name).read_bytes() == original.read_bytes()
    assert result['source'] == 'account-a/org-a' and result['destination'] == 'account-b/org-b'
    assert result['disk_verified'] and result['migration_id'] in err and 'rollback' in err
    code, out, err = run_cli('migrate', '--from', 'personal', '--to', 'work', '--dry-run', tty=False)
    assert code == 0, err
    assert json.loads(out)['already_present'] == ['local_one']
    assert (tmp_path / 'acp-home/claude-profiles.json').stat().st_mode & 0o777 == 0o600


def test_dry_run_selects_profiles_but_never_saves_aliases_or_confirms(store, run_cli):
    record(store)
    before = tree(store.root.parent.parent)
    code, out, err = run_cli('migrate', '--dry-run', answers=['2', '1'])
    assert code == 0, err
    assert json.loads(out)['dry_run'] is True
    assert tree(store.root.parent.parent) == before


@pytest.mark.parametrize('arguments', [(), ('--from', 'account-a/org-a', '--to', 'account-b/org-b'),
                                     ('--to', 'account-b/org-b', '--yes')])
def test_headless_writes_require_explicit_selectors_and_yes(store, run_cli, arguments):
    record(store)
    before = tree(store.root.parent.parent)
    code, out, err = run_cli('migrate', *arguments, tty=False)
    assert code == 1
    assert '--from' in err and '--to' in err and '--yes' in err
    assert tree(store.root.parent.parent) == before


def test_headless_explicit_yes_and_alias_resolution(store, run_cli):
    source = record(store)
    assert run_cli('alias', 'personal', 'account-a/org-a')[0] == 0
    assert run_cli('alias', 'work', 'account-b/org-b')[0] == 0
    code, out, err = run_cli('migrate', '--from', 'personal', '--to', 'work', '--yes', tty=False)
    assert code == 0, err
    assert json.loads(out)['source'] == 'account-a/org-a'
    assert (store.root / 'account-b/org-b' / source.name).exists()


def test_profile_names_use_exact_cli_account_and_org_match_without_secrets(store, run_cli, tmp_path):
    record(store, title='Older', lastActivityAt=1)
    record(store, 'recent', title='Newest', lastActivityAt=1700000000000)
    (store.root / 'account-a/org-other').mkdir()
    write(tmp_path / 'cli-config/.claude.json', {'oauthAccount': {
        'accountUuid': 'account-a', 'organizationUuid': 'org-a', 'emailAddress': 'person@example.com',
        'organizationName': 'Example Team', 'secret': 'must-not-appear'}, 'secret': 'must-not-appear'})
    code, out, err = run_cli('profiles', tty=False)
    assert code == 0, err
    profiles = json.loads(out)['profiles']
    assert profiles[0]['email'] == 'person@example.com'
    assert profiles[0]['organization'] == 'Example Team'
    assert profiles[0]['recent_sessions'][0]['title'] == 'Newest'
    assert profiles[1]['email'] is None
    assert profiles[2]['email'] is None
    assert 'must-not-appear' not in out + err
    assert not (tmp_path / 'acp-home').exists()


def test_aliases_are_scoped_to_store_and_cannot_silently_retarget(store, run_cli, tmp_path):
    assert run_cli('alias', 'work', 'account-b/org-b')[0] == 0
    code, out, err = run_cli('alias', 'work', 'account-a/org-a')
    assert code == 1 and 'already' in err
    other = tmp_path / 'other-store'
    (other / 'account-a/org-a').mkdir(parents=True)
    (other / 'account-b/org-b').mkdir(parents=True)
    store.root = other
    code, out, err = run_cli('migrate', '--from', 'account-a/org-a', '--to', 'work', '--dry-run', tty=False)
    assert code == 1 and 'Unknown' in err


def test_changed_preview_is_refused_before_destination_or_backup_writes(store, run_cli, monkeypatch):
    original = record(store)
    original_plan = store.__class__.plan
    calls = 0
    def plan(self, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            original.write_text(original.read_text().replace('"title": "one"', '"title": "edited"'))
        return original_plan(self, *args, **kwargs)
    monkeypatch.setattr(store.__class__, 'plan', plan)
    code, out, err = run_cli('migrate', answers=['2', '1', '', '', 'migrate'])
    assert code == 1 and 'changed since preview' in err
    assert not list((store.root / 'account-b/org-b').iterdir())
    assert not store.backups.exists()


def test_wizard_watches_new_destination_folder_and_rescans_without_guessing(store, run_cli, monkeypatch):
    record(store)
    def initialize_destination(_):
        (store.root / 'account-c/org-c').mkdir(parents=True)
    monkeypatch.setattr('acp.claude_profiles.time.sleep', initialize_destination)
    code, out, err = run_cli('migrate', '--dry-run', answers=['n', '3', '1'])
    assert code == 0, err
    assert json.loads(out)['destination'] == 'account-c/org-c'
    assert 'Watching for a new profile' in err
    assert not store.backups.exists()


@pytest.mark.parametrize('interruption', [EOFError, KeyboardInterrupt])
def test_terminal_interruption_cancels_without_writes(store, run_cli, monkeypatch, interruption):
    record(store)
    before = tree(store.root.parent.parent)
    def interrupted(_):
        raise interruption
    monkeypatch.setattr('acp.claude_profiles.ask', interrupted)
    code, out, err = run_cli('migrate')
    assert code == 0, err
    assert json.loads(out)['cancelled'] is True
    assert tree(store.root.parent.parent) == before


def test_alias_save_failure_reports_successful_migration_and_undo(store, run_cli, monkeypatch):
    record(store)
    def broken_save(*args):
        raise OSError('disk full')
    monkeypatch.setattr('acp.claude_profiles.Profiles.save', broken_save)
    code, out, err = run_cli('migrate', answers=['2', '1', 'personal', 'work', 'migrate'])
    assert code == 0, err
    report = json.loads(out)
    assert report['disk_verified'] and report['migration_id'] in report['undo_command']
    assert 'disk full' in report['alias_warning']
    assert (store.root / 'account-b/org-b/local_one.json').exists()


def test_explicit_terminal_selectors_still_require_confirmation(store, run_cli):
    record(store)
    before = tree(store.root.parent.parent)
    code, out, err = run_cli('migrate', '--from', 'account-a/org-a', '--to', 'account-b/org-b', answers=[''])
    assert code == 0, err
    assert json.loads(out)['cancelled'] is True
    assert tree(store.root.parent.parent) == before


def test_bad_alias_file_blocks_selection_without_overwrite(store, run_cli, tmp_path):
    path = write(tmp_path / 'acp-home/claude-profiles.json', {'version': 999, 'stores': {}})
    before = path.read_bytes()
    code, out, err = run_cli('alias', 'work', 'account-b/org-b')
    assert code == 1 and 'Invalid Claude profile' in err
    assert path.read_bytes() == before


def test_malformed_optional_cli_metadata_falls_back_to_session_titles(store, run_cli, tmp_path):
    record(store, title='Recognizable session')
    path = tmp_path / 'cli-config/.claude.json'
    path.parent.mkdir()
    path.write_text('{')
    code, out, err = run_cli('profiles', tty=False)
    assert code == 0, err
    profile = json.loads(out)['profiles'][0]
    assert profile['email'] is None
    assert profile['recent_sessions'][0]['title'] == 'Recognizable session'
