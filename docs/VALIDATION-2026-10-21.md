# Validation of main on 2026-10-21 (PRs #17, #18, #19, #20, #21)

Installed main at `8ddcf35` (merge of #20; contains the merges of #17 `a0f1b35`, #18 `89d4482`,
#19 via `954e7c0`, #21 `a211e60`) into the owner's real ACP install on this Mac and exercised each
change end to end. Every command below was run as written; results are what was observed.

Environment: macOS, Claude Code 2.1.285 at `~/.local/bin/claude`, Codex CLI 0.154.0 at
`/opt/homebrew/bin/codex`, uv 0.11.7 on PATH, uv 0.13.1 at `~/.local/share/acp/installer/uv/uv`.
Tasks ran against this worktree (`…/worktrees/agent-control-plane/install-validate-c16c1c`), see
"Trust" under Claude below for why not a scratch repo.

## Install

| Check | Command | Result |
|---|---|---|
| Before state | `acp --version`; `acp status`; `acp activity status` | 0.1.5 release wheel; running, healthy on 127.0.0.1:8765, no `comet` field; both activity sources unavailable (no hooks installed). `config.env` had `DEFAULT_PROVIDER=demo`; `~/.claude/settings.json` had no `hooks` key; `~/.codex/hooks.json` had two hadrius hooks. |
| Release check | `acp update --check` | `update_available: false` (latest release is still 0.1.5). PASS |
| Install from checkout | `acp update --source "$PWD"` | **FAIL first**: `uv could not export locked constraints from the checkout: error: Required uv version >=0.13.1 does not match the running version 0.11.7`. Nothing was stopped or changed. Cause: `find_uv()` took the uv on PATH before the installer's bundled 0.13.1. Fixed in this PR (see below). Reran with the bundled uv first on PATH: `updated: true`, `restarted: {service: true, overlay: false}` (the 0.1.5 code ran the update, so no hooks/agents report and Comet was not running before). |
| Installed package | `acp --version`; dist-info `direct_url.json` | `acp 0.1.5`; `direct_url` = `file:///…/install-validate-c16c1c`; `codex_app_server.py` present; `uv_version` fix present after the reinstall below. PASS |
| Service | `acp status` | running, reachable, healthy, pid new, url `http://127.0.0.1:8765`, `comet: {running: true}` (Comet built and started by the restart). PASS |
| Config preserved | `cmp config.env before/after` | byte-identical, still `DEFAULT_PROVIDER=demo` at that point. PASS |
| Claude hooks | diff of `~/.claude/settings.json` | only key added: `hooks` (12 events, each running `…/uv/tools/acp/bin/python -I -m acp.claude_activity --database …/claude-activity.sqlite3`); all other keys equal; `settings.acp-backup-b2b48c4bc160.json` byte-identical to the original. PASS |
| Codex hooks | diff of `~/.codex/hooks.json` | both hadrius hooks kept in place; ACP handlers appended for SessionStart, PreToolUse, SessionEnd, UserPromptSubmit, PostToolUse, PermissionRequest, SubagentStart, SubagentStop, Stop, Interrupt; `hooks.acp-backup-67a314bb21dc.json` byte-identical to the original. PASS |
| Setup report | `acp setup` | `agents.claude.command=/Users/jaswanttummala/.local/bin/claude found=true`, `agents.codex.command=/opt/homebrew/bin/codex found=true`, hooks `already installed` for both; rerunning changed neither hook file. PASS |
| Activity sources | `acp activity status` | `claude: available` (this very session's hooks fire); `codex: unavailable, "Codex hooks need trust or a new turn"` (expected until the owner trusts the handlers in Codex's `/hooks`). |
| Default provider | edit `config.env`, `acp stop && acp start` | Set `DEFAULT_PROVIDER=claude` (the only change I made to the owner's config). Board header shows "Default: Claude Code". |

Note for the owner: `acp setup` writes the detected default only when it creates `config.env`; an
existing file is never rewritten, which is why yours still said `demo` after the update. The one-line
switch is `DEFAULT_PROVIDER=claude` (done), then `acp stop && acp start`.

## Board UI (#17)

Opened http://127.0.0.1:8765 in the desktop app's built-in browser.

- Board: header has Live, Settings, API docs, "+ New task", "Default: Claude Code", "ACP 0.1.5"; lanes Planning, Todo, In Progress, Waiting on Input, Ready for Review, Deploy, Done. No "Try a demo" anywhere (page text checked). PASS
- New task dialog: Agent select offers exactly `Claude Code` and `Codex`. PASS
- Settings dialog: sections **Agents** (badge "Default: Claude Code", "Claude Code runs claude · Codex runs codex", a "Change agents" disclosure with the config.env instructions), **Linear** (Not connected, API key form), **Advanced** (Linear reply commands, Event subscriptions, Delivery diagnostics). There is **no Codex section**: the "Codex app" dispatcher section that #17 kept was removed by #21 together with the desktop dispatcher (`git log -S'Codex app' -- acp/static/index.html` → 2882291). So the dialog on main is Agents / Linear / Advanced, not Agents / Codex / Linear / Advanced. Not a bug; the PR summary is out of date.

Screenshots (board, New task, Settings, Waiting on Input, Ready for Review, Codex approval buttons,
after Accept, final board) were captured in the session; the session's permission policy refused to
commit or push image files, so they are attached to the session transcript instead of this folder.

## Comet (#18)

| Check | Command | Result |
|---|---|---|
| Started with the service | `acp status` after `acp start` | `comet.running: true`. PASS |
| On screen | `acp overlay status` | `on_screen: true, visible: true, loaded: true, frame {x:12,y:892,180×180}, level 3, all_spaces: true`, pid of `…/data/overlay/Leo.app/Contents/MacOS/Leo … http://127.0.0.1:8765`; metrics show the Claude activity feed. A desktop screenshot was not possible (`screencapture` has no screen-recording permission for this session). PASS |
| Stops with the service | `acp stop` | `comet.running: false`, no Leo process. PASS |
| Opt out | `ACP_OVERLAY=0 acp start` | service running/healthy; `comet: {running: false, skipped: "ACP_OVERLAY=0"}`, no Leo process. PASS |
| Back to normal | `acp stop && acp start` | `comet.running: true`. PASS |

## Claude background runs (#20)

Task `0289ac5b` "ACP validation: Claude background run" (provider claude, repo = this worktree). Description: request input once, then write `greeting.txt` with the answer and submit for review; on a change request update it and resubmit.

| Step | Command / observation | Result |
|---|---|---|
| Trust | first two attempts used a scratch repo under the session scratchpad and under `.acp/` in the worktree | both parked in Waiting on Input with `last_error: Workspace not trusted. Run claude in <dir> once and accept the trust prompt`; `claude --bg` only trusts directories in `~/.claude.json`, parents do not count. The run log recorded the launch failure. Moved those two tasks to Done; reran against the worktree itself. |
| Launch | `acp create … --provider claude` | `todo → in_progress` with `session_id 1f382b39-78e0-4ea3-95c0-bb54d7072887` within 5 s; run log: `backgrounded · 1f382b39 · ACP validation: Claude background run`, then `claude agents` entries with pid 42799 `status: busy`. |
| Question | `acp show` | `waiting_on_input`, blocker "Which word should the greeting file contain?"; board card in Waiting on Input, task panel shows "Waiting on your input". While parked the session is stopped (`claude agents --all` lists it as `state: stopped`, `kind: background`). PASS |
| Activity counters | `acp activity status` | `claude.running` moved 2→3→2, `last_event_at` advanced during the run. PASS |
| Answer | `acp answer 0289ac5b hello --blocker-id a7efae1f…` | `in_progress` with the **same** session id, `claude agents` lists it `busy` pid 44831; `ready_for_review` 10 s later; `greeting.txt` = `hello`; agent comments and an Outputs entry with the file path. PASS |
| Change request | `acp changes 0289ac5b "…goodbye…"` | `in_progress`, same session id, pid 46406 busy; `ready_for_review`; `greeting.txt` = `goodbye`; runs: 3 × succeeded, all `1f382b39…`. PASS |
| Visible/attachable | `claude agents --all --json`; `claude attach 1f382b39` in a Terminal tab | Listed with name "ACP validation: Claude background run", cwd = worktree, `state: done`. Attach opened the session in the terminal showing the full transcript (the change request, the `printf 'goodbye'` edit, "Ending my turn so the reviewer can respond"). The Claude desktop app's session list reads the same registry; not checked visually (no screen capture). PASS |

## Codex app-server runs (#21)

Task `a800fb66` "ACP validation: Codex app-server run" (provider codex, `CODEX_APPROVALS` unset = `acp`). Description: request input once, write `greeting-codex.txt`, then run `echo validated > ~/acp-validation-approval.txt` (outside the workspace-write sandbox), then submit.

| Step | Observation | Result |
|---|---|---|
| Launch | `in_progress`, session (thread) id `01a16063-1df1-7233-ac41-75537a0a36f8` after ~5 s; question parked the task in `waiting_on_input` after ~10 s. PASS |
| Answer | `acp answer a800fb66 banana --blocker-id 1eaa29d8…` | resumed in the same thread; 10 s later `waiting_on_input` again with `approval: {method: item/commandExecution/requestApproval, decisions: [accept, accept for session, decline]}`; blocker text shows the command `/bin/zsh -lc 'echo validated > ~/acp-validation-approval.txt'`, the directory and Codex's reason. Board card shows "Codex needs your approval" with **Accept / Accept for session / Decline** buttons; header counted "1 running" while parked (the app-server thread stays alive). PASS |
| Approve from the board | clicked **Accept** | comment `you | accept` added, task `in_progress`, `ready_for_review` ~30 s later; `~/acp-validation-approval.txt` = `validated`, `greeting-codex.txt` = `banana`; runs: 2 × succeeded, same thread id. PASS |
| Thread in Codex | `~/.codex/sessions/2026/10/21/rollout-…-01a16063-1df1-7233-ac41-75537a0a36f8.jsonl` exists | the thread is in Codex's store that the Codex app lists from; not checked visually in the app. With CLI 0.154 it is not filed under the repo's project (needs 0.163+). |
| auto_review | `acp stop && CODEX_APPROVALS=auto_review acp start` (`/api/config` → `codex_approvals: auto_review`), task `7f326ee1` with the same outside-the-sandbox write | `todo → in_progress → ready_for_review` in ~25 s with **no** blocker and `approval: null` throughout; both files written; run log has 2 `autoApprovalReview` lines and 0 `requestApproval`. Restarted normally afterwards (`codex_approvals: acp`). PASS |

Owner reminders: Codex hooks still need trusting in the Codex CLI `/hooks` menu (there is no
non-interactive way; `acp activity status` says "Codex hooks need trust or a new turn" until then).
`brew upgrade --cask codex` to 0.163+ files ACP threads under the repo's project in the Codex app.

## Hooks

After the Claude and Codex sessions above: `claude` source available with events
(`claude-activity.sqlite3`: 7 agents, 14 intervals); `codex` source still unavailable, `codex-activity.sqlite3`
has 0 agents / 0 turns because the handlers are installed but untrusted. **Expected-pending** on the owner's
`/hooks` trust, not a defect.

## Update path

| Check | Command | Result |
|---|---|---|
| Reinstall | `acp update --source "$PWD" --reinstall` (bundled uv first on PATH, since the installed code still had the bug) | `updated: true`, `restarted: {service: true, overlay: true}`, hooks `already installed` for both, `warnings: []`. `~/.claude/settings.json`, `~/.codex/hooks.json` and `config.env` byte-identical before/after; still exactly one backup of each. Service back on `http://127.0.0.1:8765` with a new pid; `comet.running: true`. PASS |
| Reinstall with the fix | same command, plain PATH (uv 0.11.7 first) | now succeeds: the installed `find_uv` picks the bundled 0.13.1. Same restart/identity checks pass. PASS |
| Tests | `uv run --locked pytest -q`; `ruff check --select E9,F63,F7,F82 acp tests` | 352 passed; All checks passed. |

## Fix in this PR

`acp/update.py`: `find_uv()` now returns the newest uv among the one on PATH and the release installer's
bundled `~/.local/share/acp/installer/uv/uv` (ties keep PATH). Before, a stale uv on PATH made
`acp update --source` fail on `uv export --locked` with the lockfile's `required-version >=0.13.1`.
Test: `test_find_uv_prefers_the_newest_uv_and_keeps_path_on_ties`.

## State left behind

- Owner's install: main `8ddcf35` plus this fix, service running on 127.0.0.1:8765, Comet up, hooks installed, `DEFAULT_PROVIDER=claude`.
- Board: three validation tasks in Ready for Review (`0289ac5b` Claude, `a800fb66` Codex, `7f326ee1` Codex auto_review) and two failed trust attempts in Done. Their files (`greeting*.txt`, `~/acp-validation-*.txt`) were deleted.
- Claude background session `1f382b39` (stopped) and two Codex threads remain in the agents' own stores.
