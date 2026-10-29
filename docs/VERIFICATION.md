# Verification record

## Claude blue unread dots — reporting limitation

Read-only inspection of the installed renderer found a separate
`epitaxy-unread-v1` Local Storage record with `state.unreadIds` and
`state.explicitUnreadIds`. It is not part of a `local_*.json` session record.
The local database after the reported account switch contained a deletion for
that key. The available older handoff backups do not establish the unread list
at the time of this switch. No unread state was restored or guessed.

Inventory, dry-run and apply now report unread state as `not_migrated`, even when
session-file hashes verify. The CLI displays this limitation at entry, before
profile selection can instruct the user to sign in to another account.
Regression assertions extend the existing inventory/dry-run, apply/rollback and
wizard-cancellation tests. Actual unread migration remains unsupported; a future
implementation needs a pre-sign-out capture, scoped restore and restart validation.

## Claude migration wizard — manual terminal checklist

Automated isolated-store tests cover numbered selection, recent-session previews,
CLI metadata labels, aliases scoped to the store, confirmation/cancellation,
noninteractive safeguards, new-folder rescanning, changed-preview refusal, and
reporting a completed migration when alias persistence fails. The CLI metadata
does not establish the current Desktop account; the user selects the destination.

1. Run `acp claude migrate --dry-run` in a host terminal. Confirm that the numbered
   profiles are recognizable from recent titles/dates and any matching CLI labels.
2. Select the destination and source. Check that the preview direction is correct
   and that no aliases, backups or session changes are written.
3. Run without `--dry-run`, enter optional names, then decline confirmation. Verify
   that nothing is migrated and no names are saved. Repeat with Ctrl-C.
4. For a disposable migration, close Claude and type `migrate`. Verify the backup,
   saved names and printed undo command, then follow the restart checklist below.
5. To initialize another destination, choose `n`, sign in and open the Code tab.
   Confirm the new folder appears in the refreshed list and still needs selection.

No real account migration or Desktop restart validation has been performed for
the wizard. The manual Desktop checks below remain necessary.

## Claude Desktop session migration — manual restart checklist

The automated suite exercises temporary session stores and transcripts, including
newer-record conflicts, independent Desktop/CLI IDs, archive/backlog merging,
interrupted writes, locks, symlinks, corrupt backups, and rollback after new work.
A read-only inventory was also checked against the local Desktop record layout.
No real account migration or Desktop restart validation was performed for this PR.

Before using the feature on valuable sessions, verify one disposable session on
the installed Desktop version. Desktop's private file format and its indexing
behavior are not a supported public API. Keep the backup until this checklist
passes; a successful disk report alone is insufficient.

1. Initialize both account/org folders by signing in to each account once. Create
   a disposable session with a worktree, a PR attachment, a subthread, and a saved
   completed/waiting status; record its IDs and paths.
2. Run inventory and a dry run, reviewing every unavailable item. Quit Desktop
   and stop CLI/background Claude processes. Keep them stopped during writes.
3. Apply the migration from a host terminal. Record the migration ID and backup
   path. Confirm `disk_verified`, the changed files and partial-verdict reasons.
4. Sign in to the destination. Confirm the restored session appears once, opens
   the existing worktree/branch, retains its PR, and opens its subthread. Confirm
   the saved status makes sense and does not imply a live approval dialog.
5. Reconnect required connectors and recreate environment/sidebar settings as
   needed. Quit and reopen Desktop again; repeat the checks. Do not modify the
   Chromium databases to force a failed migration to appear.
6. For rollback verification, quit Desktop/CLI again, run `rollback ID`, and
   confirm original destination files return. Separately edit a disposable
   destination record after migration and confirm rollback reports/skips it.

If sessions fail to appear, stop using the migrated profile, close Claude, and
roll back. Preserve the report and backup for diagnosis; do not treat the
session-copy feature as verified on that Desktop version.

## Native Comet activity overlay — 2026-10-17

Added a native macOS floating panel with the Comet critter above Running/Waiting
counts and expandable concurrency averages. The borderless translucent square
supports click-to-toggle, drag-to-move, and a bottom-right resize grip. Content
scales proportionally; position, mode, visibility, and scale persist. Four moods
follow running threads and observed 30-minute activity; reduced motion freezes
animation.

Activity counts track ACP runs and observed subagents, exclude queued agents,
and count input waits separately. Averages divide observed agent-seconds by the
full 30-minute, 1-hour, or 3-hour window, including idle time. Codex app descendants
require cooperative activity reports; this does not count unrelated app chats or
reconstruct historical subagent activity.

Validation:

- Full local check: **153 passed**, Ruff and three JavaScript syntax checks passed.
  One existing Starlette HTTPX deprecation warning remains.
- Browser overlay checks: backend-driven moods, animation/stillness, reduced
  motion, drag/keyboard controls, toggle/hide, persistence, narrow viewport,
  and API failure/slow-response recovery passed.
- Native checks: actual AppKit gestures, square resizing, content scale, saved
  scale, toggle-then-drag race recovery, restart persistence, singleton,
  visibility over another app, hide/show, and stop passed. Production-source
  pixel capture measured transparent corners (alpha 0) and translucent fill
  (alpha 0.6588). Physical multi-display/Spaces gestures remain manual checks.
- The isolated manual preview shows every mood using production assets and can
  drive synthetic activity without model calls. It remains open until Quit.
- Independent review caught a dropped-layout race after a toggle followed by
  immediate dragging. Mouse-up resynchronization and a native regression cover it.

With user approval, installed the local wheel and connected Comet to the real
ACP service. Backed up the database and previous installation, confirmed no
queued/running agents before the initial service restart, verified task IDs and
states were unchanged, and passed SQLite integrity_check. Subsequent visual
updates restarted only the overlay; the agent service stayed running. Served
assets matched the approved source and live counts matched the activity API.
Synthetic preview controls are absent from the normal service. No public release
or merge was performed for this feature.


## Published v0.1.2 and existing-tool upgrade — 2026-10-17

[Release v0.1.2](https://github.com/venti-josh/agent-control-plane/releases/tag/v0.1.2)
was published from `d97f0da76b276eb2b85f3f1f46fd42795f9ff331`.

- [Release workflow](https://github.com/venti-josh/agent-control-plane/actions/runs/39051445895)
  passed macOS/Linux checks, build, installed demo task flow, and publication.
- [Compatibility matrix](https://github.com/venti-josh/agent-control-plane/actions/runs/39051445904)
  passed Python 3.11 and 3.14 on macOS/Linux. Local `make check` passed **136 tests**,
  Ruff, and JavaScript checks; the local installed demo flow also passed.
- The literal public installer command updated the existing user installation from
  the local 0.1.1 build to public 0.1.2 after active runs settled and services stopped:
  `curl -fsSL https://github.com/venti-josh/agent-control-plane/releases/latest/download/install.sh | sh`.
- Before/after snapshots confirmed identical configuration bytes, all task/run/
  comment/artifact/desktop-run records, and dispatcher IDs in the main and test
  boards. The main board retained four tasks and restarted healthy on localhost:8765.
- The public wheel's checksum matched its published manifest. All **19 packaged
  ACP files** matched the release source and installed tool byte-for-byte.
- A Codex app task paused for input before the upgrade and resumed in the same
  chat afterward. Codex created its own managed worktree and submitted a greeting
  file for review; independent execution passed both greeting tests.
- The revision initially timed out before starting because the test and live boards
  shared a dispatcher occupied by a new live Linear task. The stale claim was
  rejected without starting duplicate work. A user-approved temporary test
  dispatcher removed that contention; retry resumed the original task chat
  (`01a14ac9-0656-7142-90e7-83866fec590d`) and the same worktree successfully.
- The complete flow reached **Done** after input, answer, implementation, review,
  and revision: three successful runs and one recorded pre-start timeout. Final
  independent verification passed **3 greeting tests**. Only the two intended
  files were added; ACP workspace/branch fields stayed empty and ACP created no
  worktree directory. The temporary test service was disconnected and stopped;
  the upgraded main board remains healthy with its original dispatcher.

Evidence: `.acp/release-0.1.2/`, including public release metadata, CI results,
installer output, data snapshots, and packaged-file comparison.

## Claude login retry — 2026-10-17

After the user signed into Claude Desktop, a real ACP Claude Code test and a fresh
standalone CLI request both returned HTTP 401: `OAuth access token has expired`.
`claude auth status` with normal Keychain access reported a saved Claude login,
but that status did not establish a valid API session. No token/API-key environment
override was set. The test made no implementation changes and reported zero model
usage on the standalone probe. The isolated test service was stopped; the main
board remained healthy. Refresh the CLI login with `claude auth login` before
retrying. Evidence: `.acp/claude-live/report.json` and its run log.

This attempted the CLI integration. The documented
[Claude Desktop deep link](https://support.claude.com/en/articles/14729294-open-claude-desktop-with-a-link)
prefills a composer for a person to submit; it does not establish automatic
desktop execution for ACP.

## Agent-owned workspaces and simpler prompts — 2026-10-17

- **136 regression tests passed**, plus Ruff and JavaScript syntax checks. The
  existing Starlette HTTPX deprecation warning remains.
- ACP no longer creates branches/worktrees or requires a particular branch.
  Both CLI adapters start in the selected repository (or an older session's saved
  directory), leaving workspace decisions to the agent. The desktop task agent
  uses normal app tools. Task prompts contain context and a short reporting
  contract, without a planning, implementation, or Git checklist.
- Real subprocess/HTTP/Git adapter tests exercised input, answer, review, and
  revision for simulated Codex and Claude executables. The simulated agents chose
  their own worktree; ACP created no workspace and retained the explicit session.
  Live Claude execution remains deferred at the user's earlier request.
- The installed wheel passed a real Codex app flow: input → answer → implementation
  → review → revision → Done. All three runs succeeded in the same chat,
  `01a14aa5-7667-74c2-a153-96e6cde6c492`.
- Codex itself created a managed worktree at
  `~/.codex/worktrees/agent-workspace-greeting/agent-control-plane` on
  `codex/agent-workspace-greeting`. ACP's workspace and branch fields remained
  empty, no `workspace.created` event was emitted, and no ACP worktree directory
  was created. Revision reused that same agent-owned workspace.
- Independent execution of the final greeting tests passed **3 tests**; only the
  two intended files were added in the test worktree. No commits or pushes.
- All 19 packaged ACP files matched source and the installed tool. Existing board
  configuration and task records were preserved. The updated form explicitly says
  that the agent chooses its workspace. The isolated test service was stopped;
  the connected main board remains on localhost:8765.

Evidence: `.acp/agent-workspace-live/report.json`, `greeting-tests.log`, installation
snapshots, and `.acp/desktop-live/pytest-workspace.log`. This supersedes the earlier
ACP-owned workspace design below. These changes were subsequently published in
v0.1.2, verified above.

## Codex app dispatcher, unpublished local build — 2026-10-17

- Final regression suite: **134 passed**, with the existing Starlette HTTPX
  deprecation warning. Ruff and JavaScript syntax checks passed.
- Supported `codex queue` woke the connected app dispatcher. Its native app tools
  created task chats and continued the same chat on answers and revision requests.
  No private IPC, UI automation, global hooks, or hook trust changes were used.
- A real greeting task went through input, answer, implementation, review, revision,
  and Done in one app chat and retained worktree. Three workflow runs succeeded
  after an initial permission-guidance failure was corrected. Independent execution
  of the resulting greeting tests passed **2 tests**.
- A fresh task through the installed wheel began without a repository selection.
  The dispatcher chose the correct repository, created a new app chat, and observed
  its read-only review submission and successful completion. The test task was
  then moved to Done, and the isolated service on port 8877 was stopped.
- Regression coverage includes duplicate claims, stale callbacks, cancellation,
  recovery after creation but before binding, resumed preparation failure,
  transaction-local claim validation, and app-run serialization while other
  providers can use additional worker slots.
- The local wheel was installed on the main board at port 8765, connected to the
  dispatcher. Existing configuration bytes and all four stored tasks, session IDs,
  runs, comments, and artifacts were preserved. All 19 packaged ACP files matched
  source and the installed tool byte-for-byte.

Codex must remain open. Each run uses a dispatcher coordination turn, including
follow-ups. ACP cancellation revokes results; the user must stop the actual turn
in Codex. Unacknowledged runs block overlapping retries. The app start deadline
is 120 seconds, bounded by the configured run timeout.

Evidence: `.acp/desktop-live/report.json`, `pytest-final.log`, installation snapshots,
and retained test worktrees. The tested local build is still labeled 0.1.1 and is
not a new release. The public GitHub v0.1.1 installer remains unchanged.

## Published v0.1.1 and existing-tool upgrade — 2026-10-16

[Release v0.1.1](https://github.com/venti-josh/agent-control-plane/releases/tag/v0.1.1)
was published from `dbc3ed4dd94a48b2cabea4232276d2927cce4b6e` and was the latest
release at verification time.

- [Release workflow](https://github.com/venti-josh/agent-control-plane/actions/runs/38997409955)
  passed macOS/Linux checks, build, installed demo task flow, and publication.
- [Python compatibility matrix](https://github.com/venti-josh/agent-control-plane/actions/runs/38997409964)
  passed Python 3.11 and 3.14 on macOS/Linux. Local `make check` passed 119 tests,
  Ruff, and JavaScript checks before tagging.
- The literal public `curl .../releases/latest/download/install.sh | sh` command
  updated the existing user installation after an idle check and `acp stop`.
  Configuration and API key bytes were unchanged; all three visible tasks retained
  their runs, comments, artifacts, session IDs, repositories, and worktrees.
  ACP restarted healthy on localhost:8765, connected to the same Linear owner.
- A separate public v0.1.0 installation created a waiting demo task, stopped,
  upgraded using the latest GitHub command, and resumed the same session through
  review, revision, and Done. All three runs succeeded; configuration and history
  survived. This was a simulated agent test, not another live-provider claim.
- The public wheel matched all 18 installed/source ACP files byte-for-byte.
  SHA-256: `8eb6a299f9276fc6c8bc73dab0d73bb03b2cba2d07055ffaec897a40bfa7258b`.

Evidence: `.acp/github-existing-update-0.1.1/report.json` and `installer.log`;
`.acp/github-upgrade-0.1.1/report.json` and per-version installer logs.

v0.1.1 uses CLI execution. App-owned Codex/Claude chats are a subsequent request
and are not represented as a feature of this release.

## v0.1.1 pre-release Linear setup and owner sync — 2026-10-16

The checks in this section were performed on the local candidate before publication.

- `make check`: **119 tests passed**, plus Ruff and JavaScript syntax checks.
  The existing upstream Starlette HTTPX deprecation warning remains.
- Guided UI and agent CLI both configured the live Linear team successfully.
  The simplified Settings screen shows account, team, agent, and Sync now;
  workflow mapping, webhooks, imports, subscriptions, and diagnostics are collapsed.
- Boards are pinned to the API key's viewer ID. Live tests verified that only
  issues assigned to that user with the `acp` label appear; removing either
  hides the task, and restoration keeps its history.
- Real Codex selected the intended repository automatically, asked for input,
  resumed from a Linear reply, produced a verified file, revised it in the same
  session, and synced Done. TRY-5 verified actual signed webhook delivery;
  TRY-7 verified the flow with the webhook disabled and polling only.
- Live discovery exposed Linear's creation timestamps sometimes having
  `updatedAt` slightly before `createdAt`; regression coverage now preserves
  new human commands. A separate regression covers replies posted after
  reassignment but before the next scan.
- The release-asset installer test passed install, service startup, demo task
  input/review/revision/Done, stop, reinstall, and data preservation. Evidence:
  `.acp/install-smoke-20261016-225943/report.json`.
- The installed wheel then passed a fresh real Codex/Linear flow on **TRY-8**:
  all three runs succeeded in one session, the expected two-line file was verified,
  and Done synced. Owner/label removal and restoration also passed. Evidence:
  `.acp/linear-installed-report.json`. All 18 packaged ACP files matched the
  current source and installed wheel byte-for-byte.
- Claude live execution was deferred at the user's request after its saved OAuth
  token had expired. Mocked provider tests pass; this is not a live Claude claim.
- The temporary webhook, tunnel, and isolated test service were removed/stopped.
  The installed app uses polling and remains private on localhost:8765.

Source-run evidence: `.acp/linear-live-report.json`,
`.acp/linear-owner-report.json`, retained task worktrees/logs, and
`.acp/settings-simplified.png`. Synthetic tasks made no source changes or pushes.

## Published v0.1.0 — 2026-10-16

Source and release tag are public at commit
`fa270c52e8367d72e92524f4e46c533783f57202`.
[Release v0.1.0](https://github.com/venti-josh/agent-control-plane/releases/tag/v0.1.0)
contains the wheel, pinned installer, constraints, version file and checksums.

The first GitHub check exposed an invalid `setup-uv@v9` action alias. Both workflows
now pin the verified v10.3.0 commit. The tag was corrected before any release was
published. The subsequent checks passed:

- [Release workflow](https://github.com/venti-josh/agent-control-plane/actions/runs/38994939856):
  **96 tests passed** on both macOS and Linux, plus Ruff/JavaScript checks and the
  full installed demo task flow on each platform. Publication succeeded.
- [Python matrix](https://github.com/venti-josh/agent-control-plane/actions/runs/38994939952):
  macOS/Linux with Python 3.11 and 3.14 passed.
- The public wheel was downloaded and all 16 packaged ACP source/UI files were
  compared byte-for-byte with the release tag. All matched. Published wheel SHA-256:
  `3feb5b9ac9d9edae181a77c4a5f0f46f92fcb90e2b061b7a9e79e24c316f836a`.

The exact public single-command installation was then tested on macOS:

```sh
curl -fsSL https://github.com/venti-josh/agent-control-plane/releases/latest/download/install.sh | sh
```

The test used fresh isolated ACP/uv tool directories, hid uv from PATH, and used
no local release URL override or GitHub credentials in the download command.
The installer bootstrapped uv and fetched the public release assets successfully.
The installed CLI started the service, opened the correct URL, and ran a real
Codex task through input, implementation, revision and explicit Done. All three
agent runs succeeded in one retained session/worktree, and independent unittest
and exact-output assertions passed. An open event stream did not prevent stop;
reinstall preserved configuration and the completed task. The test service was
stopped at the end.

Task: `d2ea092c-7340-43f1-bde7-4382677031f1`.
Session: `01a147b1-7a3d-7f13-aa3e-31671d297eb3`.
Local evidence: `.acp/github-install-smoke-20261016-222923/report.json`,
`installer.log`, `task.json`, `unittest.log`, and the retained task worktree/logs.
The public-download test harness is `.acp/github-install-smoke.py`; it uses the
same installed task flow as `scripts/install-smoke.py`, replacing the local
asset server with the literal GitHub download pipeline above.

## Release installer and background service — 2026-10-16

The installed CLI now supports setup, start, stop, status, open, and logs. The
foreground server shares the same authenticated instance identity as the detached
service. Persistent user configuration is independent of the current directory.

- Final `make check`: **96 passed**, no skips. Ruff and JavaScript syntax checks
  passed; the existing upstream Starlette HTTPX deprecation warning remains.
- New regression coverage: concurrent and repeated start; occupied port; stale
  metadata; unauthenticated stop; task persistence; open EventSource shutdown;
  active agent cleanup; foreground control; unhealthy/unreachable/stopped instance
  routing; config-relative paths; damaged release assets; isolated helper imports
  when a task repository contains a conflicting `acp` package; macOS zombie-only
  process-group cleanup.
- `sh scripts/build-release.sh`: built the wheel and version-pinned installer,
  exported locked runtime constraints, and generated SHA-256 checksums. Wheel
  inspection confirmed packaged UI assets and no runtime `.env` or database files.
- Final `scripts/install-smoke.py --bootstrap`: **passed** against the actual
  release assets over local HTTP, with uv hidden from PATH. Installed uv and ACP,
  checked browser opening, completed the three-run demo input/review cycle,
  refused reinstall while running, stopped with an open event stream, then
  reinstalled and verified configuration and completed-task persistence.
  Evidence: `.acp/install-smoke-20261016-221919/report.json`.
- An intermediate final-wheel real task submitted its revision but failed cleanup
  with macOS EPERM. A separate real-process reproduction showed macOS rejects
  signals to zombie-only groups. The new regression failed before the fix;
  cleanup now checks both process-group ID and process state, retaining errors
  for live members and unsuccessful process lookups.
- Independent code review found and resolved service routing and helper-import
  isolation defects. Follow-up review found no remaining blockers.

Final authenticated installed-package verification also passed:

- Task `de59b8ea-e4cb-4e62-b863-13fcb66b8c50`, session
  `01a147aa-16a8-7d22-b6d5-416a48574d82`.
- Three real Codex runs: ask for punctuation, implement and submit, then trim
  whitespace and resubmit after explicit review feedback. All runs succeeded
  with the same session, branch, and worktree.
- The harness independently reran unittest and checked exact normal/padded-name
  outputs, explicitly marked Done, stopped with an open event stream, reinstalled
  the wheel, restarted, and verified Done persisted. The source checkout stayed
  clean. The test service was stopped afterward.
- Evidence: `.acp/install-smoke-20261016-222125/report.json`, `task.json`,
  `unittest.log`, and the retained task worktree and raw run logs.
- Final wheel SHA-256:
  `e6992d9d12d4bef1379f5ca12c5003897f492579a5cf8b6174624c7b055ba89c`.

The release workflow checks macOS and Linux before publishing a version tag.
At the end of that implementation phase, tests had run locally on macOS and
publication was pending. The published-release record above supersedes that status.

## ACP rename and uv setup — 2026-10-16

The package, CLI, MCP server, environment variables, event headers, correlation
markers, task branches, runtime paths, UI, and tests now use ACP naming.
Historical verification evidence is preserved without rewriting its contents in
`.acp/history/pre-rename-verification.tar.gz` (ignored local runtime data).

Checks recorded for the earlier rename on macOS:

- `make setup`: installs through `uv sync --locked` and validates configuration.
  Fresh installation and repeated setup preserve `.env` and leave `uv.lock`
  unchanged. The installed CLI reports `usage: acp`.
- `make check`: **79 passed, no skips**, including native MCP stdio and actual
  HTTP/subprocess/Git lifecycles with fake provider executables. Ruff's
  syntax/undefined-name checks and JavaScript syntax check also passed.
- `uv pip check`: all installed packages are compatible.
- Native Google Chrome browser smoke: **passed**, including seven lanes,
  real HTTP/EventSource updates, input/review loops, create/move, escaped text,
  subscriptions, and a mobile viewport. `docs/preview.png` was regenerated from
  the ACP UI and visually checked.

The project pins Python 3.14.8 in `.python-version`, requires uv 0.13.1+, and
pins uv 0.13.1 in CI. uv 0.13.1 was verified to download that Python version into
an isolated managed installation. Earlier uv versions could use the existing
local interpreter but could not download the pin, which is why setup enforces
the newer uv minimum.

Browser command used:

```bash
ACP_SCREENSHOT=docs/preview.png \
CHROMIUM_EXECUTABLE='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
uv run --locked --group browser python tests/browser_smoke.py
```

The native browser test uses an isolated temporary database and simulated demo
agents. It does not call or charge a model. An upstream Starlette deprecation
warning about its HTTPX test client remains in the Python suite.

The GitHub Actions matrix uses uv on macOS/Linux with Python 3.11 and 3.14.
Those remote jobs have not been published or run on GitHub. Dependencies,
Python pin, and commands follow the [uv project workflow](https://docs.astral.sh/uv/concepts/projects/sync/)
and [official GitHub Actions guidance](https://docs.astral.sh/uv/guides/integration/github/).

## Earlier authenticated Codex workflow

Before the rename, a real task was verified on macOS with Python 3.14.8,
MCP SDK 1.30.0, and `codex-cli 0.162.0-alpha.17.2`, using the installed CLI
credentials and a disposable committed Git repository:

1. The agent recorded a plan and asked which greeting punctuation to use.
   Its first run stopped successfully in Waiting on Input.
2. An explicit answer resumed the same session and worktree. The agent
   implemented a greeting function, added a unittest, ran it, and submitted
   the file for review.
3. An explicit change request resumed the same session again. The agent added
   whitespace trimming and a regression test, ran both tests, and resubmitted.
4. The harness independently reran both tests and checked exact normal and
   whitespace-padded outputs. All three runs succeeded. It explicitly moved
   the task to Done and stopped its isolated server. The source checkout
   remained clean.

Task ID: `a70bc8bb-38c6-487c-a23d-bd8256ad9e98`.
Session: `01a14752-d8a9-7201-917b-b13970047ff8`.
The original report, task snapshot, events, raw logs, worktree files, and harness
are in the local history archive. File contents in the archive retain their
original names and paths as historical evidence; they are not a migrated live
workspace.

That test exposed missing Codex task-tool approval configuration. The adapter
now explicitly approves only the four task-scoped MCP tools, retains the
workspace-write sandbox, and gates other tools using the
[official per-tool MCP settings](https://developers.openai.com/codex/mcp/).
Post-rename native MCP and runner tests exercise the ACP namespace; no additional
authenticated model run was made for the rename.

No PR, push, merge, deployment, or live external integration write was performed.
Authenticated Claude and live Linear/GitHub webhooks remain unverified. Deploy
and Done are workflow states; automatic deployment, backup, retention, worktree
cleanup, and future schema migrations are not implemented.

## Linear evidence uploads — October 17, 2026

The source implementation now snapshots explicitly linked local evidence when
an ACP comment/review is submitted and uploads it before mirroring the comment.
Markdown documents become downloadable links; image embeds retain their syntax.
Desktop and MCP instructions tell agents to report evidence through ACP.

- Original shared-checkout suite: `164 passed` in 31.93 seconds, including
  unrelated pending comment-threading tests.
- Isolated attachment-only PR branch: `158 passed` in 34.52 seconds, with one
  existing Starlette/httpx deprecation warning. The suite required localhost and
  process permissions; the initial sandbox-only run could not exercise
  daemon/runner tests.
- Evidence suite: 22 tests covering snapshots, retries after restart, uncertain
  comment responses, upload failures, edits, missing/oversized/non-regular files,
  code examples, escaped links, titled links, and inbound Linear comments.
- Required Ruff checks, JavaScript syntax check, and `git diff --check` passed.
- Wheel and source distribution built. The wheel contains `acp/evidence.py` and
  declares the locked CommonMark parser dependency.
- Independent review found Markdown parsing issues; regressions reproduced them
  before fixes. The final focused review reported no remaining findings.
- Live Linear verification uploaded a 418-byte Markdown report and a 68-byte
  synthetic PNG fixture after deleting the source files. Authenticated downloads
  matched the snapshots byte-for-byte. A subsequent Linear GraphQL read confirmed
  both asset URLs in the [ticket comment](https://linear.app/tryprincipal/issue/TRY-11/update-instructions-and-functionality-to-upload-attachments#comment-abe3c0c9).

The PNG is an upload fixture, not a product screenshot. Live verification used
the new source uploader with an isolated database and published through the
active ACP run. The installed running service was not replaced or restarted;
installing the updated package and restarting it remains necessary for normal
agent reports to use this behavior. Chat-only attachments and old comments are
not automatically imported. Existing unrelated comment-threading edits were
preserved in the shared checkout and excluded from the attachment PR.

### PR #2 review follow-up

Rebased onto `7caea6e` from `main`, preserving both the new activity-reporting
instructions and the attachment instructions in the desktop prompt. Addressed
all three review findings: ignore hidden inline HTML comments, recognize balanced
link labels, and reject non-local authorities in file review URLs. Regression
coverage also preserves literal HTML syntax inside code and screenshots wrapped
in links.

Final rebased suite: `189 passed` in 36.16 seconds, including 36 evidence tests,
with the existing Starlette/httpx deprecation warning. Required Ruff checks,
JavaScript syntax, and diff checks passed. Focused independent review found no
remaining issues in the fixes. No installed-service upgrade was performed.

### Markdown MIME type portability

Both failed macOS/Python 3.11 CI jobs uploaded Markdown as
`application/octet-stream`: that runtime's default MIME registry has no `.md`
entry. ACP now supplies `text/markdown` explicitly for `.md` and `.markdown`
(case-insensitive), preserving system detection and binary fallback for other
files. Regression tests reproduce a missing system mapping.

Full macOS suites passed on Python 3.11.17 (`193 passed` in 30.86 seconds) and
Python 3.14.8 (`193 passed` in 36.87 seconds), including 40 evidence tests. Both
retain the existing Starlette/httpx warning. Ruff, JavaScript syntax, diff checks,
and focused review passed.
