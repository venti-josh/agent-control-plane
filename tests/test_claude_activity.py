"""Claude Code hook observations: count every local surface, fence late events, never save content."""
import json
import os
import subprocess
import sys

import pytest


def tracker(tmp_path, monkeypatch, home=None):
    from acp.claude_activity import ClaudeActivity
    clock = [10000.0]
    monkeypatch.setattr('acp.codex_activity.time.time', lambda: clock[0])
    monkeypatch.setattr('acp.claude_activity.owner_alive', lambda owner: owner.startswith('alive'))
    return ClaudeActivity(tmp_path / 'claude.sqlite3', home or tmp_path / 'no-claude-home'), clock


def event(name, session='chat', prompt='p1', **fields):
    payload = dict(hook_event_name=name, session_id=session, transcript_path='/PRIVATE/transcript.jsonl',
                   cwd='/PRIVATE/project', **fields)
    if prompt is not None:
        payload['prompt_id'] = prompt
    return payload


def test_counts_sessions_subagents_and_waits_without_saving_content(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    assert monitor.summary()['available'] is False
    monitor.record(event('SessionStart', prompt=None, source='startup', model='PRIVATE-MODEL'), 'alive')
    assert monitor.summary()['available'] is True and monitor.summary()['running'] == 0
    monitor.record(event('UserPromptSubmit', prompt='PRIVATE PROMPT'.replace(' ', '-')), 'alive')
    monitor.record(event('UserPromptSubmit', session='other', prompt='q1'), 'alive')
    monitor.record(event('SubagentStart', agent_id='child', agent_type='Explore'), 'alive')
    monitor.record(event('PreToolUse', agent_id='child', agent_type='Explore', tool_name='Grep',
                         tool_use_id='t1', tool_input={'pattern': 'PRIVATE'}), 'alive')
    summary = monitor.summary()
    assert summary['running'] == 3 and summary['subagents'] == 1
    monitor.record(event('PreToolUse', tool_name='AskUserQuestion', tool_use_id='ask',
                         tool_input={'questions': 'PRIVATE'}), 'alive')
    monitor.record(event('PermissionRequest', session='other', prompt='q1', tool_name='Bash',
                         tool_input={'command': 'PRIVATE'}), 'alive')
    summary = monitor.summary()
    assert summary['running'] == 1 and summary['waiting_on_input'] == 2
    assert summary['approval_status_unconfirmed'] == 1
    monitor.record(event('PostToolUse', session='other', prompt='q1', tool_name='Read', tool_use_id='r'), 'alive')
    assert monitor.summary()['waiting_on_input'] == 2  # another tool cannot clear this approval
    monitor.record(event('PostToolUse', tool_name='AskUserQuestion', tool_use_id='ask', tool_response='SECRET'), 'alive')
    monitor.record(event('PostToolUseFailure', session='other', prompt='q1', tool_name='Bash', error='SECRET'), 'alive')
    assert monitor.summary()['running'] == 3 and monitor.summary()['waiting_on_input'] == 0
    monitor.record(event('SubagentStop', agent_id='child', agent_type='Explore', last_assistant_message='PRIVATE'), 'alive')
    monitor.record(event('Stop', last_assistant_message='PRIVATE'), 'alive')
    monitor.record(event('Stop', session='other', prompt='q1'), 'alive')
    assert monitor.summary()['running'] == monitor.summary()['waiting_on_input'] == 0
    for path in tmp_path.iterdir():
        assert b'PRIVATE' not in path.read_bytes() and b'SECRET' not in path.read_bytes()


def test_prompt_id_fences_late_events_and_replayed_prompts(tmp_path, monkeypatch):
    monitor, clock = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit'), 'alive')
    clock[0] += 100
    monitor.record(event('Stop'), 'alive')
    monitor.record(event('UserPromptSubmit'), 'alive')  # replayed prompt
    monitor.record(event('PostToolUse', tool_name='Bash'), 'alive')
    assert monitor.summary()['running'] == 0
    monitor.record(event('UserPromptSubmit', prompt='p2'), 'alive')
    # StopFailure is not awaited by Claude Code, so it can land after the next prompt.
    monitor.record(event('StopFailure', prompt='p1', error='rate_limit'), 'alive')
    monitor.record(event('Notification', prompt='p1', notification_type='idle_prompt'), 'alive')
    assert monitor.summary()['running'] == 1
    clock[0] += 50
    monitor.record(event('Stop', prompt='p2'), 'alive')
    assert monitor.summary()['averages']['30m'] == pytest.approx(150 / 1800)


def test_new_prompt_while_running_continues_without_double_counting(tmp_path, monkeypatch):
    monitor, clock = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit'), 'alive')
    clock[0] += 10
    monitor.record(event('UserPromptSubmit', prompt='p2'), 'alive')  # a queued message
    assert monitor.summary()['running'] == 1
    clock[0] += 10
    monitor.record(event('Stop', prompt='p2'), 'alive')
    assert monitor.summary()['averages']['30m'] == pytest.approx(20 / 1800)


def test_stop_continuation_and_resumed_subagent_reopen_only_on_fresh_work(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit'), 'alive')
    monitor.record(event('Stop'), 'alive')
    monitor.record(event('PostToolUse', tool_name='Bash'), 'alive')
    assert monitor.summary()['running'] == 0
    monitor.record(event('PreToolUse', tool_name='Bash'), 'alive')  # a Stop hook kept the turn going
    assert monitor.summary()['running'] == 1
    monitor.record(event('Stop'), 'alive')
    monitor.record(event('SubagentStart', agent_id='child', agent_type='general-purpose'), 'alive')
    monitor.record(event('SubagentStop', agent_id='child', agent_type='general-purpose'), 'alive')
    monitor.record(event('PostToolUse', agent_id='child', tool_name='Read'), 'alive')
    assert monitor.summary()['running'] == 0
    monitor.record(event('PreToolUse', agent_id='child', tool_name='Read'), 'alive')
    assert monitor.summary()['subagents'] == 1


def test_root_stop_keeps_background_subagent_and_session_end_closes_both(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit'), 'alive')
    monitor.record(event('SubagentStart', agent_id='child', agent_type='general-purpose'), 'alive')
    monitor.record(event('Stop'), 'alive')
    assert monitor.summary()['running'] == 1
    monitor.record(event('SessionEnd', prompt=None, reason='other'), 'old-owner')
    assert monitor.summary()['running'] == 1  # another process cannot end this one's session
    monitor.record(event('SessionEnd', prompt=None, reason='prompt_input_exit'), 'alive')
    assert monitor.summary()['running'] == 0


def test_usage_limit_failure_ends_the_turn_and_its_subagents(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit'), 'alive')
    monitor.record(event('SubagentStart', agent_id='child', agent_type='Explore'), 'alive')
    monitor.record(event('StopFailure', error='rate_limit', last_assistant_message='API Error: PRIVATE'), 'alive')
    summary = monitor.summary()
    assert summary['running'] == 0 and summary['available'] is True
    monitor.record(event('UserPromptSubmit', prompt='p2'), 'alive')
    assert monitor.summary()['running'] == 1


def test_idle_prompt_recovers_from_interrupts_and_other_notifications_are_ignored(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    monitor.record(event('Notification', notification_type='permission_prompt', message='PRIVATE'), 'alive')
    assert monitor.summary()['running'] == 0
    monitor.record(event('UserPromptSubmit'), 'alive')
    monitor.record(event('PermissionRequest', tool_name='Bash'), 'alive')
    monitor.record(event('Notification', notification_type='auth_success', message='PRIVATE'), 'alive')
    assert monitor.summary()['waiting_on_input'] == 1
    # The user rejected the approval (an interrupt, so no Stop) and walked away.
    monitor.record(event('Notification', notification_type='idle_prompt', message='PRIVATE'), 'alive')
    assert monitor.summary()['running'] == monitor.summary()['waiting_on_input'] == 0


def test_missing_prompt_id_still_tracks_turns(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit', prompt=None), 'alive')
    monitor.record(event('PreToolUse', prompt=None, tool_name='Bash'), 'alive')
    assert monitor.summary()['running'] == 1
    monitor.record(event('Stop', prompt=None), 'alive')
    assert monitor.summary()['running'] == 0
    monitor.record(event('UserPromptSubmit', prompt=None), 'alive')
    assert monitor.summary()['running'] == 1


def test_silent_turn_goes_unknown_then_expires_instead_of_blocking_counts(tmp_path, monkeypatch):
    # Interrupts end a Claude turn without any hook while the process stays alive.
    monitor, clock = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit'), 'alive')
    clock[0] += 60
    monitor.record(event('PreToolUse', tool_name='Bash'), 'alive')
    clock[0] += 400
    summary = monitor.summary()
    assert summary['available'] is False and summary['unknown'] == 1
    assert summary['message'] == 'Claude activity uncertain: 1 silent agent'
    clock[0] += 1500
    summary = monitor.summary()
    assert summary['available'] is True and summary['unknown'] == summary['running'] == 0
    assert summary['averages']['3h'] == pytest.approx(60 / 10800)
    monitor.record(event('PreToolUse', tool_name='Bash'), 'alive')
    assert monitor.summary()['running'] == 1


def test_dead_owner_closes_and_unidentified_owner_is_unknown(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit'), 'dead')
    assert monitor.summary()['running'] == 0
    monkeypatch.setattr('acp.claude_activity.owner_alive', lambda owner: None)
    monitor.record(event('UserPromptSubmit', session='other'), '')
    assert monitor.summary()['unknown'] == 1


def registry(home, pid, **fields):
    (home / 'sessions').mkdir(parents=True, exist_ok=True)
    (home / 'sessions' / f'{pid}.json').write_text(json.dumps({'pid': pid, 'name': 'PRIVATE', **fields}))


def test_registry_idle_after_last_hook_ends_an_interrupted_turn(tmp_path, monkeypatch):
    home = tmp_path / 'claude-home'
    monitor, clock = tracker(tmp_path, monkeypatch, home)
    monkeypatch.setattr('acp.claude_activity.owner_alive', lambda owner: owner == '42:start')
    monitor.record(event('UserPromptSubmit'), '42:start')
    registry(home, 42, sessionId='chat', status='idle', statusUpdatedAt=(clock[0] - 5) * 1000)
    assert monitor.summary()['running'] == 1  # an idle status from before this prompt proves nothing
    registry(home, 42, sessionId='other', status='idle', statusUpdatedAt=(clock[0] + 5) * 1000)
    assert monitor.summary()['running'] == 1
    registry(home, 42, sessionId='chat', status='busy', statusUpdatedAt=(clock[0] + 5) * 1000)
    assert monitor.summary()['running'] == 1
    (home / 'sessions' / '42.json').write_text('{not json')
    assert monitor.summary()['running'] == 1
    clock[0] += 20
    registry(home, 42, sessionId='chat', status='idle', statusUpdatedAt=(clock[0] - 10) * 1000)
    summary = monitor.summary()
    assert summary['running'] == 0 and summary['available'] is True
    assert summary['averages']['30m'] == pytest.approx(10 / 1800)
    for path in tmp_path.iterdir():
        assert path.is_dir() or b'PRIVATE' not in path.read_bytes()


def test_unknown_events_malformed_ids_and_internal_agents_produce_no_counts(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    for payload in [[], {}, event('Unrecognized'), event('UserPromptSubmit', session=''),
                    event('UserPromptSubmit', agent_id='bad\nid'),
                    event('SubagentStart', agent_id='suggestion', agent_type=''),
                    event('PostToolUse', tool_name='Bash'), event('Stop')]:
        monitor.record(payload, 'alive')
    assert monitor.summary()['running'] == 0


def test_owner_is_the_claude_process_not_the_desktop_app(monkeypatch):
    from acp import claude_activity
    tree = {100: (101, 'Mon 1', '/bin/sh'), 101: (102, 'Mon 2', '/Users/x/claude-code/2.1/claude.app/Contents/MacOS/claude'),
            102: (103, 'Mon 3', '/Applications/Claude.app/Contents/Helpers/disclaimer'),
            103: (1, 'Mon 4', '/Applications/Claude.app/Contents/MacOS/Claude')}
    monkeypatch.setattr(claude_activity, 'process_info', tree.get)
    monkeypatch.setattr(claude_activity.os, 'getppid', lambda: 100)
    assert claude_activity.owner_identity() == '101:Mon 2'
    tree[101] = (102, 'Mon 2', 'node')  # A Node-launched CLI keeps its own process as owner.
    assert claude_activity.owner_identity() == '101:Mon 2'


def test_merge_never_lets_a_stale_or_missing_source_blank_the_other():
    from acp.claude_activity import merge, unavailable

    def source(name, observed=True, available=True, running=0, waiting=0, unknown=0, average=0.0, message='ok'):
        return dict(source=name, available=available, running=running, subagents=0, waiting_on_input=waiting,
                    approval_status_unconfirmed=0, unknown=unknown, averages={'30m': average, '1h': average, '3h': average},
                    message=message, observed_since=1.0 if observed else None, last_event_at=2.0 if observed else None)
    codex_stale = source('codex_hooks', available=False, running=1, unknown=15, average=.5)
    claude = source('claude_hooks', running=2, waiting=1, average=1.0)
    data = merge(codex_stale, claude)
    assert data['available'] is True and data['running'] == 3 and data['waiting_on_input'] == 1
    assert data['averages']['30m'] == pytest.approx(1.5)
    assert data['message'] == 'Codex uncertain: 15 silent'
    assert data['sources']['codex'] is codex_stale and data['sources']['claude'] is claude
    data = merge(source('codex_hooks', running=1), source('claude_hooks', running=2))
    assert data['message'] == ''
    data = merge(source('codex_hooks', observed=False, available=False), claude)
    assert data['available'] is True and data['running'] == 2 and data['message'] == 'ok'
    data = merge(codex_stale, source('claude_hooks', observed=False, available=False))
    assert data['available'] is False and data['message'] == 'ok'
    data = merge(unavailable('codex_hooks', 'Codex'), claude)
    assert data['available'] is True and data['running'] == 2
    assert data['message'] == 'ok · Codex activity unavailable'
    assert merge(source('codex_hooks', observed=False), source('claude_hooks', observed=False))['available'] is False


def test_api_reports_both_sources_and_survives_a_broken_one(client, settings, monkeypatch):
    from acp.claude_activity import ClaudeActivity
    from acp.codex_activity import CodexActivity
    monkeypatch.setattr('acp.claude_activity.owner_alive', lambda owner: True)
    monkeypatch.setattr('acp.codex_activity.owner_alive', lambda owner: True)
    ClaudeActivity(settings.data_dir / 'claude-activity.sqlite3').record(event('UserPromptSubmit'), 'owner')
    CodexActivity(settings.data_dir / 'codex-activity.sqlite3').record(
        dict(hook_event_name='UserPromptSubmit', session_id='codex-chat', turn_id='t'), 'owner')
    data = client.get('/api/activity').json()
    assert data['available'] is True and data['running'] == 2
    assert data['sources']['claude']['running'] == data['sources']['codex']['running'] == 1
    (settings.data_dir / 'codex-activity.sqlite3').write_bytes(b'not a database')
    data = client.get('/api/activity').json()
    assert data['available'] is True and data['running'] == 1
    assert data['sources']['codex']['message'] == 'Codex activity unavailable'


def install_paths(tmp_path):
    home = tmp_path / 'claude'
    home.mkdir()
    return home, home / 'settings.json', tmp_path / 'data' / 'claude-activity.sqlite3'


def test_installer_writes_only_hooks_is_idempotent_and_uninstall_restores(tmp_path):
    from acp.claude_activity import EVENTS, install_hooks, uninstall_hooks
    home, path, database = install_paths(tmp_path)
    original = {'permissions': {'allow': ['mcp__pencil'], 'deny': ['Bash(rm:*)']}, 'effortLevel': 'high',
                'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'echo existing'}]}],
                          'PreCompact': [], 'Notification': [{'matcher': 'permission_prompt', 'hooks': []}]}}
    text = json.dumps(original, indent=2) + '\n'
    path.write_text(text)
    path.chmod(0o644)
    preview = install_hooks(home, database, sys.executable, dry_run=True)
    assert path.read_text() == text and '+' in preview['diff'] and 'acp.claude_activity' in preview['diff']
    install_hooks(home, database, sys.executable)
    first = path.read_bytes()
    install_hooks(home, database, sys.executable)
    assert path.read_bytes() == first
    assert oct(path.stat().st_mode & 0o777) == '0o644'
    backups = list(home.glob('settings.acp-backup-*.json'))
    assert len(backups) == 1 and backups[0].read_text() == text
    data = json.loads(first)
    assert {k: v for k, v in data.items() if k != 'hooks'} == {k: v for k, v in original.items() if k != 'hooks'}
    assert data['hooks']['Stop'][0] == original['hooks']['Stop'][0]
    assert set(EVENTS) <= set(data['hooks'])
    notification = data['hooks']['Notification'][-1]
    assert notification['matcher'] == 'idle_prompt'
    # Claude Code runs the generated command: it ingests only its event and prints nothing.
    command = data['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']
    assert data['hooks']['UserPromptSubmit'][0]['hooks'][0]['timeout'] <= 10
    result = subprocess.run(command, shell=True, input=json.dumps(event('UserPromptSubmit')),
                            text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and result.stdout == ''
    data['hooks']['Stop'].append({'hooks': [{'type': 'command', 'command': 'echo added-later'}]})
    data['model'] = 'added-later'
    path.write_text(json.dumps(data))
    uninstall_hooks(home, database)
    remaining = json.loads(path.read_text())
    assert [g['hooks'][0]['command'] for g in remaining['hooks']['Stop']] == ['echo existing', 'echo added-later']
    remaining['hooks']['Stop'].pop()
    del remaining['model']
    assert remaining == original
    assert uninstall_hooks(home, database) == {'removed': False}


def test_installer_handles_missing_files_symlinks_and_invalid_settings(tmp_path):
    from acp.claude_activity import install_hooks, uninstall_hooks
    home, path, database = install_paths(tmp_path)
    install_hooks(home, database, sys.executable)
    assert 'hooks' in json.loads(path.read_text())
    uninstall_hooks(home, database)
    assert json.loads(path.read_text()) == {}
    path.unlink()
    target = tmp_path / 'dotfiles.json'
    target.write_text(json.dumps({'theme': 'dark'}))
    path.symlink_to(target)
    install_hooks(home, database, sys.executable)
    assert path.is_symlink() and 'hooks' in json.loads(target.read_text())
    uninstall_hooks(home, database)
    assert path.is_symlink() and json.loads(target.read_text()) == {'theme': 'dark'}
    for broken in ['{not json', '[]', '{"hooks": []}']:
        target.write_text(broken)
        with pytest.raises(ValueError):
            install_hooks(home, database, sys.executable)
        assert target.read_text() == broken


def test_hook_process_failure_never_blocks_claude_or_echoes_payload(tmp_path):
    result = subprocess.run([sys.executable, '-m', 'acp.claude_activity', '--database', str(tmp_path / 'x.db')],
                            input='not-json SECRET', text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and result.stdout == ''
    assert 'SECRET' not in result.stderr


def test_cli_dry_run_prints_diff_and_status_combines_sources(tmp_path):
    home = tmp_path / 'claude'
    env = {**os.environ, 'ACP_CONFIG_FILE': str(tmp_path / 'none.env'), 'ACP_DATA_DIR': str(tmp_path / 'data')}
    run = lambda *args: subprocess.run([sys.executable, '-m', 'acp.cli', 'activity', *args],  # noqa: E731
                                       env=env, text=True, capture_output=True, timeout=30)
    result = run('install', '--target', 'claude', '--claude-config-dir', str(home), '--dry-run')
    assert result.returncode == 0, result.stderr
    assert 'acp.claude_activity' in result.stdout and not (home / 'settings.json').exists()
    result = run('status')
    assert result.returncode == 0, result.stderr
    assert set(json.loads(result.stdout)['sources']) == {'codex', 'claude'}
