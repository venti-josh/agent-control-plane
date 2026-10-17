"""Hook observations must count agents, fence late events, and never save content."""
import importlib.util
import json
import os
import subprocess
import sys

import pytest


def tracker(tmp_path, monkeypatch):
    assert importlib.util.find_spec('acp.codex_activity'), 'Codex-wide hook tracker is missing'
    from acp.codex_activity import CodexActivity
    clock = [10000.0]
    monkeypatch.setattr('acp.codex_activity.time.time', lambda: clock[0])
    monkeypatch.setattr('acp.codex_activity.owner_alive', lambda owner: owner == 'alive')
    return CodexActivity(tmp_path / 'activity.sqlite3'), clock


def event(name, session='chat', turn='turn-1', **fields):
    return dict(hook_event_name=name, session_id=session, turn_id=turn, **fields)


def test_counts_external_chats_children_and_waits_without_board_tasks(tmp_path, monkeypatch):
    monitor, clock = tracker(tmp_path, monkeypatch)
    assert monitor.summary()['available'] is False
    monitor.record(event('UserPromptSubmit', prompt='PRIVATE PROMPT'), 'alive')
    monitor.record(event('UserPromptSubmit', session='other'), 'alive')
    monitor.record(event('SubagentStart', agent_id='child'), 'alive')
    assert monitor.summary()['running'] == 3
    assert monitor.summary()['subagents'] == 1
    monitor.record(event('PreToolUse', tool_name='request_user_input', tool_use_id='input'), 'alive')
    assert monitor.summary()['running'] == 2
    assert monitor.summary()['waiting_on_input'] == 1
    monitor.record(event('PostToolUse', tool_name='request_user_input', tool_use_id='input', tool_response='SECRET'), 'alive')
    monitor.record(event('PermissionRequest', session='other', tool_name='Bash'), 'alive')
    assert monitor.summary()['running'] == 2
    assert monitor.summary()['waiting_on_input'] == 1
    monitor.record(event('PostToolUse', session='other', tool_name='apply_patch'), 'alive')
    assert monitor.summary()['waiting_on_input'] == 1  # another tool cannot clear this approval
    monitor.record(event('PostToolUse', session='other', tool_name='Bash'), 'alive')
    monitor.record(event('SubagentStop', agent_id='child', last_assistant_message='PRIVATE'), 'alive')
    monitor.record(event('Stop'), 'alive')
    monitor.record(event('Interrupt', session='other'), 'alive')
    assert monitor.summary()['running'] == monitor.summary()['waiting_on_input'] == 0
    for path in tmp_path.iterdir():
        assert b'PRIVATE' not in path.read_bytes() and b'SECRET' not in path.read_bytes()


def test_duplicate_and_late_hooks_cannot_revive_finished_turns(tmp_path, monkeypatch):
    monitor, clock = tracker(tmp_path, monkeypatch)
    start = event('UserPromptSubmit')
    monitor.record(start, 'alive'); monitor.record(start, 'alive')
    clock[0] += 100
    monitor.record(event('Stop'), 'alive')
    monitor.record(start, 'alive')
    monitor.record(event('PostToolUse', tool_name='Bash'), 'alive')
    assert monitor.summary()['running'] == 0
    monitor.record(event('UserPromptSubmit', turn='turn-2'), 'alive')
    monitor.record(event('Interrupt', turn='turn-1'), 'alive')
    assert monitor.summary()['running'] == 1
    clock[0] += 50
    monitor.record(event('Stop', turn='turn-2'), 'alive')
    assert monitor.summary()['averages']['30m'] == pytest.approx(150 / 1800)


def test_root_stop_does_not_erase_still_running_child_and_session_end_closes_both(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit'), 'alive')
    monitor.record(event('SubagentStart', agent_id='child'), 'alive')
    monitor.record(event('Stop'), 'alive')
    assert monitor.summary()['running'] == 1
    monitor.record(event('SessionEnd', turn=None), 'alive')
    assert monitor.summary()['running'] == 0


def test_stale_observation_is_unknown_and_dead_process_is_not_waiting(tmp_path, monkeypatch):
    monitor, clock = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit'), 'alive')
    clock[0] += 301
    summary = monitor.summary()
    assert summary['available'] is False and summary['unknown'] == 1
    monitor.record(event('PreToolUse', tool_name='Bash', tool_use_id='fresh'), 'alive')
    assert monitor.summary()['available'] is True
    monkeypatch.setattr('acp.codex_activity.owner_alive', lambda owner: False)
    assert monitor.summary()['running'] == monitor.summary()['waiting_on_input'] == 0


def test_unknown_events_and_malformed_ids_do_not_produce_counts(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    for payload in [[], {}, event('Unrecognized'), event('UserPromptSubmit',session=''),
                    event('UserPromptSubmit',turn=None)]:
        monitor.record(payload, 'alive')
    assert monitor.summary()['available'] is False
    assert monitor.summary()['running'] == 0


def test_async_question_does_not_pause_agent_after_tool_returns(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit'), 'alive')
    monitor.record(event('PreToolUse', tool_name='request_user_input_async', tool_use_id='q'), 'alive')
    monitor.record(event('PostToolUse', tool_name='request_user_input_async', tool_use_id='q'), 'alive')
    assert monitor.summary()['running'] == 1 and monitor.summary()['waiting_on_input'] == 0


def test_api_ignores_failed_waiting_board_tasks(client):
    svc=client.app.state.service
    task=svc.create_task('Failed task', state='todo')
    run=svc.claim_next()
    svc.finish_run(run['id'], 'failed', 'Never started')
    result=client.get('/api/activity').json()
    assert result['waiting_on_input'] == 0
    assert result['source'] == 'codex_hooks'
    assert result['available'] is False


def test_installer_preserves_other_hooks_is_idempotent_and_removes_only_ours(tmp_path, monkeypatch):
    assert importlib.util.find_spec('acp.codex_activity'), 'Codex hook installer is missing'
    from acp.codex_activity import install_hooks, uninstall_hooks
    home=tmp_path/'codex';home.mkdir()
    original={'description':'existing','hooks':{'Stop':[{'hooks':[{'type':'command','command':'echo existing'}]}]}}
    path=home/'hooks.json';path.write_text(json.dumps(original))
    database=tmp_path/'data/activity.sqlite3'
    install_hooks(home,database,sys.executable)
    first=path.read_bytes();install_hooks(home,database,sys.executable)
    assert path.read_bytes()==first
    data=json.loads(first)
    assert data['hooks']['Stop'][0]==original['hooks']['Stop'][0]
    # Codex runs the generated command: it must ingest only its event, with no model-facing output.
    command=data['hooks']['UserPromptSubmit'][0]['hooks'][0]['command']
    result=subprocess.run(command,shell=True,input=json.dumps(event('Stop')),text=True,capture_output=True)
    assert result.returncode==0 and json.loads(result.stdout)=={}
    data['hooks']['Stop'].append({'hooks':[{'type':'command','command':'echo added-later'}]})
    path.write_text(json.dumps(data))
    uninstall_hooks(home,database)
    remaining=json.loads(path.read_text())
    assert [g['hooks'][0]['command'] for g in remaining['hooks']['Stop']]==['echo existing','echo added-later']
    assert remaining['description']=='existing'


def test_hook_process_failure_does_not_block_codex_or_echo_payload(tmp_path):
    result=subprocess.run([sys.executable,'-m','acp.codex_activity','--database',str(tmp_path/'x.db')],
                          input='not-json SECRET',text=True,capture_output=True)
    assert result.returncode==0
    assert json.loads(result.stdout)=={}
    assert 'SECRET' not in result.stderr


def test_child_uses_own_turn_for_waits_and_parent_turn_for_stop(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit'), 'alive')
    monitor.record(event('SubagentStart', agent_id='child'), 'alive')
    monitor.record(event('PreToolUse', session='child',turn='child-turn',tool_name='request_user_input',tool_use_id='q'), 'alive')
    assert monitor.summary()['waiting_on_input'] == 1
    assert monitor.summary()['running'] == 1
    monitor.record(event('SubagentStop', agent_id='child'), 'alive')
    assert monitor.summary()['waiting_on_input'] == 0
    monitor.record(event('SubagentStart', agent_id='child'), 'alive')
    assert monitor.summary()['running'] == 1  # replayed start cannot reopen it


def test_stale_gap_is_not_counted_in_average_when_hooks_return(tmp_path, monkeypatch):
    monitor, clock = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit'), 'alive')
    clock[0]+=30
    monitor.record(event('PreToolUse',tool_name='Bash'), 'alive')
    clock[0]+=600
    assert monitor.summary()['available'] is False
    monitor.record(event('PostToolUse',tool_name='Bash'), 'alive')
    clock[0]+=10
    monitor.record(event('Stop'), 'alive')
    assert monitor.summary()['averages']['30m']==pytest.approx(40/1800)


def test_install_uninstall_preserves_unrelated_empty_hook_groups(tmp_path):
    from acp.codex_activity import install_hooks, uninstall_hooks
    home = tmp_path/'codex'; home.mkdir()
    original = {'hooks': {'Stop': [{'matcher': 'existing', 'hooks': []}], 'SessionStart': []}}
    path = home/'hooks.json'; path.write_text(json.dumps(original))
    database = tmp_path/'activity.db'
    install_hooks(home, database, sys.executable)
    uninstall_hooks(home, database)
    assert json.loads(path.read_text()) == original


def test_old_owner_session_end_does_not_close_current_session(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit'), 'alive')
    monitor.record(event('SessionEnd'), 'old-owner')
    assert monitor.summary()['running'] == 1


def test_child_followup_preserves_parent_and_subagent_identity(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit'), 'alive')
    monitor.record(event('SubagentStart', agent_id='child'), 'alive')
    monitor.record(event('PreToolUse', session='child', turn='c1', tool_name='Bash'), 'alive')
    monitor.record(event('Stop', session='child', turn='c1'), 'alive')
    monitor.record(event('UserPromptSubmit', session='child', turn='c2'), 'alive')
    assert monitor.summary()['subagents'] == 1
    monitor.record(event('SessionEnd'), 'alive')
    assert monitor.summary()['running'] == 0


def test_stop_continuation_allows_fresh_tools_but_not_late_results(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    monitor.record(event('UserPromptSubmit'), 'alive')
    monitor.record(event('Stop'), 'alive')
    monitor.record(event('PostToolUse', tool_name='Bash'), 'alive')
    assert monitor.summary()['running'] == 0
    monitor.record(event('PreToolUse', tool_name='Bash'), 'alive')
    assert monitor.summary()['running'] == 1
    monitor.record(event('Interrupt'), 'alive')
    monitor.record(event('PreToolUse', tool_name='Bash'), 'alive')
    assert monitor.summary()['running'] == 0


def test_approval_wait_is_explicitly_an_unconfirmed_observation(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    monitor.record(event('PermissionRequest', tool_name='Bash'), 'alive')
    data = monitor.summary()
    assert data['approval_status_unconfirmed'] == 1
    assert 'unconfirmed' in data['message']
    monitor.record(event('PostToolUse', tool_name='Bash'), 'alive')
    assert monitor.summary()['approval_status_unconfirmed'] == 0


def test_subagent_stop_can_continue_at_a_fresh_tool_start(tmp_path, monkeypatch):
    monitor, _ = tracker(tmp_path, monkeypatch)
    monitor.record(event('SubagentStart', agent_id='child'), 'alive')
    monitor.record(event('PreToolUse', session='child', turn='child-turn', tool_name='Bash'), 'alive')
    monitor.record(event('SubagentStop', agent_id='child'), 'alive')
    assert monitor.summary()['running'] == 0
    monitor.record(event('PreToolUse', session='child', turn='child-turn', tool_name='Bash'), 'alive')
    assert monitor.summary()['subagents'] == 1
    monitor.record(event('SubagentStop', agent_id='child'), 'alive')
    assert monitor.summary()['running'] == 0
