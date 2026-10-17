# Verification record

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
The current local tests ran on macOS. GitHub CI and public release download are
not yet verified: the public repository is still empty and no release was
published during implementation. The documented installer URL becomes usable
after the initial source push and matching release tag are published.

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
