# ACP

A small, local-first Kanban for **one person**, **Claude Code / Codex**, and optionally **one Linear workspace**. One Python backend, SQLite, and a browser UI with no build step. No accounts, roles, Redis, or microservices.

## Install

ACP runs on macOS and Linux (including WSL). Install the
[latest GitHub release](https://github.com/venti-josh/agent-control-plane/releases/latest)
with one command:

```sh
curl -fsSL https://github.com/venti-josh/agent-control-plane/releases/latest/download/install.sh | sh
```

The installer downloads the release wheel and locked runtime constraints,
checks their SHA-256 checksums, and installs the `acp` command with uv. If uv
0.13.1+ is unavailable, it installs a private copy for ACP. uv supplies Python
3.14 when needed. No repository clone or environment activation is required.
The release assets must be publicly downloadable for this command.

The installer prints the executable's absolute path. If its directory is not
already on PATH, add the printed directory to PATH (usually `$HOME/.local/bin`).
It creates configuration but does not automatically start a service or agent.

```sh
acp start          # Background service; closing the terminal is safe
acp open           # Open the board in your browser
acp status         # JSON: running, reachable, healthy, URL, PID, log path
acp logs           # Last 100 log lines; use --lines 500 for more
acp stop           # Stop ACP and CLI runs; stop desktop turns in Codex
```

`acp start` is idempotent. It binds to localhost port 8765 by default; an occupied
port produces an error without stopping the other application. Use
`acp start --port 8767` to choose a port, or `--port 0` for an available port.
Other ACP commands discover the running service's URL and refuse requests if that
instance is unavailable. `acp open` requires a
reachable service; `acp serve` remains available for foreground operation and uses the same instance
identity, so `acp status` and `acp stop` work with it too. Worker health reports
the last worker error until restart; it does not change which instance receives commands.

The service keeps running after the terminal closes. It does **not** restart
automatically after a reboot or crash; run `acp start` again. Stopping interrupts
CLI runs and retains their session references for explicit retry. Active Codex desktop runs retain their run and chat identities across a service restart. Agent files stay where the agent created them. Closing a browser tab
does not stop tasks. Interactive API documentation is at the reported URL plus
`/docs`. Choose **Try a demo** to exercise input and review without an agent account.

### Comet’s macOS activity overlay

Run `uv run --locked acp overlay start` with the ACP service running to open the
native floating macOS panel. It stays above ordinary apps and can follow Spaces.
Drag anywhere on the square tile to move it; click without dragging to expand or
minimize it. Drag the small bottom-right grip to resize; the pet and counters
scale together, and your size is remembered. Both sizes share one translucent,
borderless surface.
The comet in the macOS menu bar provides Show, Hide, Reset position, and Quit.
Position and display preferences are saved locally. Building the panel once
requires Apple's command line tools (`xcode-select --install`).

To test without touching live agents, run:

```sh
ACP_OVERLAY_UI=1 ACP_CONFIG_FILE=/dev/null uv run --locked python tests/mac_overlay.py
```

After native checks pass, open the printed `controls` URL. It shows Dead, Sad,
Happy, and Rainbow side by side in both tile sizes. The desktop buttons drive
simulated activity in the native panel within five
seconds. The preview stays open until **Quit Comet**. Only this test
server exposes mood controls; the normal overlay always follows actual activity.

The tile observes **all local Codex and Claude Code chats and subagents**, including
chats outside ACP, through user-level lifecycle hooks. Install the hook handlers once:

```sh
acp activity install --target all --dry-run   # review the exact settings diff first
acp activity install --target all             # or --target codex / --target claude
# In the Codex CLI: open /hooks, review and trust the ACP command handlers.
# Then start a new Codex turn and a new Claude Code session.
acp activity status                           # combined; --target codex|claude for one
```

`--target` defaults to `codex`, so existing scripts keep their behavior.

Codex requires explicit review of the exact hook commands; ACP never modifies
hook trust. The installer preserves existing handlers and saves a backup beside
`~/.codex/hooks.json`. Use `--codex-home PATH` for a custom Codex home. To remove
only ACP's handlers, run `acp activity uninstall`. Install from the installed tool
(or a persistent environment), since hooks use its absolute Python path.

**Claude Code.** The Claude Code CLI, the Claude desktop app's Code tab, and the IDE
extensions all read user hooks from `~/.claude/settings.json` (or
`$CLAUDE_CONFIG_DIR/settings.json`; `--claude-config-dir PATH` overrides both). The
installer adds only ACP's command entries under `hooks`, never touches permissions or
any other setting, preserves existing handlers (including a symlinked settings file),
keeps the file mode, and saves the original once as `settings.acp-backup-*.json`.
Reinstalling is a no-op; `acp activity uninstall --target claude` removes only ACP's
entries. New sessions pick up the hooks; sessions already open may keep the hooks
they started with. The hook prints nothing (stdout from `SessionStart` and
`UserPromptSubmit` hooks would become model context), always exits 0, and takes about
0.1 s.

Claude has no turn ID, so each event's `prompt_id` fences turns: a late `Stop`,
`StopFailure`, or idle notification from an earlier prompt never closes a newer one.
`StopFailure` (API errors such as a usage limit) ends the turn and its subagents.
Approvals (`PermissionRequest`) and `AskUserQuestion` count as waiting until the tool
returns. An interrupt, or rejecting an approval, ends a turn without any hook: the
CLI's idle notification, the next prompt, the session ending, Claude Code's own
session registry (`sessions/<pid>.json`, read only for an explicit idle status of the
same process and session), or the silence timeouts below settle it. The owner is the
`claude` process that ran the hook; the desktop app's long-lived `Claude` process is
never used. Stored data matches Codex's and lives in `claude-activity.sqlite3`.

Each source is summarized separately and then combined. `GET /api/activity` returns
the combined counts plus `sources.codex` and `sources.claude`; the status line shows
the split (for example `Codex 1 · Claude 2 running`). A source that was never observed,
is silent, or fails to read never blanks the other: counters go unavailable only when
no source is trustworthy.

Running counts include observed root chats and subagents; waiting counts reflect
explicit input/approval hooks. Idle, finished, failed, and queued board tasks are
excluded. Async questions do not pause an agent. Hooks do not provide an approval
resolution event: a wait can remain observed until the tool returns, even after
approval. Hosted tools may not emit every lifecycle hook. Activity silent for five
minutes becomes **unavailable**, rather than presenting potentially stale counts;
a fresh event from that agent restores it. Codex runs no hook when a turn ends in an
error such as a usage limit, so an agent silent for 30 minutes is treated as finished
and stops blocking the counts; a later tool start in the same turn is observed again.
Claude agents follow the same rules. A dead Codex or Claude process clears its counts.

Averages divide observed running agent-seconds by the full 30-minute, 1-hour, or
3-hour window. Earlier activity and already-running chats cannot be reconstructed;
coverage begins when trusted hooks receive new events. Counters refresh every five
seconds. Only identifiers, process ownership, lifecycle states/tool names, and
timestamps are stored in `codex-activity.sqlite3` and `claude-activity.sqlite3`; no
prompts, transcripts, or tool outputs are retained. The hooks run locally, make no
network calls, and never approve tools or modify agent responses.

Comet plays dead below a 30-minute average of 1 (X eyes, limp tail, no motion),
sighs with drooping ears and tears from 1 to below 2, flies happily from 2 to
below 3, and leaves a rainbow trail at 3 or more. System reduced-motion settings disable
animation. Failed activity requests show unavailable counters until recovery.

### Storage and configuration

By default ACP uses `$XDG_DATA_HOME/acp`, or `$HOME/.local/share/acp`:

- `config.env`: provider commands, defaults, and optional integration settings.
- `data/`: SQLite database, run logs and private service metadata.
- `data/service.log`: server output (`service.log.1` is rotated at startup above 10 MiB).

`acp setup` creates configuration if missing and preserves existing values.
Use `ACP_HOME=/absolute/path` to isolate a whole installation's state, or
`ACP_CONFIG_FILE=/absolute/path/config.env` and `ACP_DATA_DIR=/absolute/path/data`
for separate overrides. Relative data and repository paths in a config file are
resolved against that file's directory. Environment variables take precedence over configuration.
ACP never automatically loads a `.env` from the current directory. Use the same
overrides for every command targeting a particular instance.

To keep existing checkout data, explicitly set `ACP_CONFIG_FILE` to that checkout's
`.env` and make its `ACP_DATA_DIR` absolute before starting the service. Existing
worktrees contain absolute paths: do not relocate them by copying the data directory.
Stop the old foreground server before using the same data with `acp start`.

### Upgrade and uninstall

Stop ACP, rerun the install command, then start it again. Installation refuses to
replace the selected instance while its managed service is running. Stop any other
instances or foreground servers using the same installed executable before upgrading.
Configuration and task data survive reinstall. Use the release installer for updates;
`uv tool upgrade acp` is not the update path for downloaded wheel installations.

Pin a release by using its URL instead of `latest`:

```sh
curl -fsSL https://github.com/venti-josh/agent-control-plane/releases/download/v0.1.5/install.sh | sh
```

To uninstall, run `acp stop`, then `uv tool uninstall acp`. If the installer supplied
uv, use `$HOME/.local/share/acp/installer/uv/uv` (or the corresponding `ACP_HOME` path).
Task data is retained; delete it separately only when no longer needed.

### Develop from source

Requires [uv 0.13.1+](https://docs.astral.sh/uv/getting-started/installation/).
Python 3.14.8 is pinned for development; the application supports Python 3.11+.

```sh
make setup         # Locked environment; preserves existing .env
make serve         # Explicitly uses this checkout's .env
make check
```

`make hooks` likewise uses the checkout configuration. For other source commands,
set `ACP_CONFIG_FILE="$PWD/.env"` before `uv run --locked acp ...`. Without Make,
run `sh scripts/setup.sh`, then
`ACP_CONFIG_FILE="$PWD/.env" uv run --locked acp serve`.
To use another supported interpreter, set `UV_PYTHON=3.11` for setup and execution.
Keep `.env`, `.acp`, and `.venv` out of Git. Real tasks need a local Git repository; install and authenticate Codex or Claude separately.

### Build and publish a release

Set matching versions in `pyproject.toml` and `acp/__init__.py`, update `uv.lock`,
then run:

```sh
sh scripts/build-release.sh
uv run --locked python scripts/install-smoke.py
# Optional authenticated, real Codex task (uses your CLI account):
uv run --locked python scripts/install-smoke.py --live
# Exercise the bootstrap path with uv hidden from PATH:
uv run --locked python scripts/install-smoke.py --bootstrap
```

Assets are written to `dist/release/`: wheel, locked runtime constraints, VERSION,
SHA256SUMS, and a version-pinned installer. The smoke test serves these exact assets
locally over HTTP, installs into isolated tool directories, exercises a task through
input and review, then checks stop/restart/reinstall persistence. It saves evidence
under `.acp/install-smoke-*` and stops its service when finished.

Merge the version change into `main`. The **CI and release** workflow runs only on
pushes to `main`; feature branches, pull requests, and tag pushes do not run CI.
It checks macOS and Linux with Python 3.11 and 3.14. If `vX.Y.Z` is not already
published, it builds and smoke-tests the installed demo on both platforms, then
creates the tag at the tested commit and publishes the exact verified Linux assets.
Do not create a tag manually. Subsequent pushes with the same published version run
checks but skip publication. To release again, increase both versions and run
`uv lock`, then merge into `main`.

Publication is serialized and requires every check and smoke test to pass. API
errors fail the workflow. Rerun a failed workflow from GitHub Actions; if an upload
left an unfinished draft, inspect and remove that draft first. Existing tags must
match the tested commit and are never moved. No package is published to PyPI.
Local asset tests do not establish that a GitHub release has been published or
downloaded successfully.

## What is implemented

| Lane | Behavior |
| --- | --- |
| Planning | Define or edit a task. No run starts. |
| Todo | Atomically queue a run. Remains Todo until a process actually starts. |
| In Progress | The runner owns this state. The task's own agent plans and implements. |
| Waiting on Input | A nonempty blocking comment is required in the same database transaction. The agent pauses. |
| Ready for Review | Output and summary are saved; the agent pauses. New human comments or explicit changes-requested feedback queue another run. |
| Deploy | Deployment handoff. A merged linked PR moves here when the task was awaiting review. |
| Done | Explicit completion. Nothing is automatically merged, deployed, or marked done. |

The seven lanes are task states, not process states. Runs separately record queued, running, succeeded, failed, cancelled, or interrupted. A successfully paused run can appear under Waiting on Input: it successfully asked a question, not completed the task.

The UI supports drag-and-drop, a keyboard-accessible lane selector, comments, answer-and-resume, change requests, outputs, run logs, task edits, stop/unqueue, session reset, event subscriptions, and Linear import. State/comment changes publish persisted events; the UI receives server-sent events.

## Safe command retries

Task creation, comments (including answer-and-resume), input requests, review
submissions, and change requests accept an `Idempotency-Key` header. ACP commits
the mutation and its response together; replaying the same key and payload returns
the original result, including after restart. Reusing a key with different input
returns 409. CLI commands expose `--request-id` and MCP writes expose `request_id`
for an explicit command identity. Unresolved CLI/MCP writes automatically retain
their request ID in a private local SQLite journal across process restarts. Retrying
the identical server, path, and payload reuses that ID; errors also print it. Only
payload hashes and opaque IDs are saved. Successful responses or definitive client
errors clear the entry, so a later successful command can express a new intent.
Keep the journal and use the same server URL and payload when recovering a lost
response; changing them creates a different intent. `ACP_COMMAND_JOURNAL` can select
a journal path; the default is `client-commands.sqlite3` under the ACP user home.

The browser retains an unresolved command key across reloads in the same tab.
Browser, CLI, and MCP transport retries reuse their key, make at most three attempts,
and refuse protected writes if the server lacks receipt support. Long Retry-After windows
return control to the caller. Other writes are not automatically replayed. This is
not a claim that every API operation or external side effect is exactly-once.

Explicit answer-and-resume requires the expected blocker comment ID (`in_reply_to`
in the API, `--blocker-id` in the CLI; obtain it with `acp show TASK_ID`). Replies
to an earlier blocker are retained as context with `resume_queued: false`, without
scheduling work. The browser pins an answer draft to its original blocker across
live updates and explains when a stale answer was saved without resuming.

## Run tasks in the Codex app

Queue acceptance, dispatcher claim, and task start are separate milestones. A saved
connection means **configured**, not ready. Accepted work stays active while awaiting
the dispatcher or task check-in, including across a Codex app restart. After two
minutes ACP adds one actionable delay comment; it does not expire the approved work.
The execution timeout begins only when the task chat checks in and stops when
it submits input or review. Dispatcher acknowledgment is tracked separately: a
delay preserves the result and queued follow-ups, and after two minutes exposes
an acknowledgment warning and same-chat reconciliation. Task details show
the milestones, and pending desktop work is excluded from the board's running count.

Use **Retry delivery** (or **Reconcile handoff** after a claim) in task details, or
`acp desktop recover RUN_ID`. This queues delivery for the same active run, with a
30-second retry throttle. It does not create a new run or reapply approval. If Codex
still has not claimed it, open the configured dispatcher chat and resolve its resume
or approval request. Queueing alone is not a documented desktop wake guarantee.
Before sending a task prompt, preparation grants one send and persists its handoff
marker. Repeated notifications resume preparation only before that grant; after it,
they observe the bound chat or require evidence of the exact marker to reconcile
an uncertain handoff. Missing partial history never authorizes a blind resend. Canceled and terminal runs stay revoked;
retry those through the normal answer-and-resume flow to create a new run.

Both Codex app and ACP service restarts retain active desktop runs. ACP reattaches
to the same run and chat, preserving the original execution deadline. Dispatcher
notification failures have three durable attempts with backoff; permanent failures
and exhausted budgets require explicit recovery. Restarting does not reset the
budget or bypass a pending retry deadline. Health and task details expose waiting
work, unresolved handoffs, and exhausted delivery instead of reporting them ready.
Waiting desktop work retains the existing serialized dispatcher slot until it starts
or is canceled; the UI never reports waiting time as task execution.

Available in v0.1.2 and later. Earlier releases use CLI execution and ACP-managed worktrees.

Install and sign in to Codex, then open ACP **Settings → Codex app → Copy setup
prompt**. Paste that prompt into Codex once. The agent creates a dedicated ACP
dispatcher chat and connects it to this board. You can also get the prompt with:

```sh
acp desktop instructions
acp desktop status
```

Keep the Codex app open with the ACP dispatcher chat open in a window: Codex
delivers queued messages only to chats that are open, so a run queued while the
dispatcher chat is closed waits until you open it. Codex tasks then run automatically in app chats, with the
same chat resumed for answers and requested revisions. New human comments on
In Progress tasks steer their existing active app chat automatically. The dispatcher
checks for comments between 10-second waits and forwards them through the app's
message tool, without waiting for the task to finish or starting another ACP run.
Comments on Waiting on Input or Ready for Review tasks queue a continuation in
the same chat. Todo comments join queued task context. Planning, Deploy, and Done
comments remain notes; edits, bot comments, and mirrored agent updates do not
trigger work. CLI providers receive comments in a follow-up after the active process
finishes; in-flight steering is available for connected Codex app tasks.

Delivery is recorded per comment. An interrupted dispatcher never blindly resends
an uncertain message: it must inspect the target chat for the comment ID and confirm
delivery before acknowledging it. New feedback blocks an agent's stale review/input
submission until delivery is confirmed. Cancellation still revokes the run; it never
turns an old comment into a new automatic retry. Linear webhooks deliver changes as
they arrive; polling-only installations discover them on the existing 60-second
scan. App scheduling and tool latency also affect delivery time.

The agent manages its working directory and any worktree. The
supported `codex queue` command wakes the dispatcher; the dispatcher uses the app's
thread tools to create/continue task chats and observe completion. It performs a
small coordination turn for each run, including follow-ups. A connected dispatcher
coordinates one Codex app run at a time; extra worker slots remain available to
other providers. No lifecycle hooks,
private IPC, desktop UI automation, or hook-trust bypass are installed.

The agent chooses a repository from ACP's local catalog when the task has no
repository. Normal Codex app permissions still apply. A task's In Progress state
begins only when its app chat calls back, and a run finishes only after the
dispatcher acknowledges the chat's actual completion. Queue delivery is not
completion. Existing CLI sessions are not adopted as app-owned chats; the first
app run gets a new chat with task history and any previously saved workspace path as context.

**Stopping:** stop the active turn in Codex. **Cancel tracking** in ACP revokes the
run's result permissions; it cannot forcibly interrupt a desktop turn. An
unacknowledged previous app run blocks overlapping retries, session resets, and
provider changes until its dispatcher acknowledges completion. After an ACP
restart, inspect the retained app chat before retrying. If the dispatcher itself
was interrupted, ask it to reconcile its last run and acknowledge the completed
or interrupted task chat using the command in its dispatch prompt.

To connect an existing dedicated dispatcher, an agent can run
`acp desktop connect --thread CHAT_UUID`. Use `acp desktop disconnect` when there
are no unresolved app runs. Existing app tasks require reconnection or an explicit
session reset before CLI execution. Claude remains a CLI provider.

## Use a real agent through the CLI

Install and authenticate the **Claude Code CLI** or **Codex CLI** on the same machine and OS user that runs ACP. Verify the CLI works in your terminal first. ACP uses that CLI's authentication; it does not read ChatGPT connector credentials.

Set these in the `config.env` path printed by `acp setup` (or `.env` for source development), then restart ACP:

```dotenv
DEFAULT_PROVIDER=claude
DEFAULT_REPO=/absolute/path/to/your/git-repository
```

Use `codex` instead of `claude` to change the default. Each task can override the agent and repository. Model names are optional: `CLAUDE_MODEL` / `CODEX_MODEL`; otherwise the provider CLI chooses its configured default.

Create a task with acceptance criteria and put it into Todo. ACP passes the task,
comments, outputs, and latest input to the agent. The agent chooses its working
directory and any worktree using its normal workflow and repository instructions.
ACP does not create branches, worktrees, or copies of the repository.

For tasks linked to Linear, the agent prompt includes the issue URL and asks the
agent to include its issue identifier in new branch names, for example
`codex/eng-42-short-description`. Repository and user naming conventions still
apply. Existing branches and PRs keep their names, and tasks without a Linear
identifier use the agent's normal naming. This supports PR linking through
[Linear's GitHub integration](https://linear.app/integrations/github).

CLI agents start in the selected repository. Existing tasks from earlier versions
start in their saved workspace for session compatibility, without enforcing a
branch or recreating missing files. Codex app tasks use their app chat's workspace
and normal app tools. A workspace chosen by the agent can be reported in a progress
comment or submitted as an output path; ACP does not manage its lifecycle.

The task's session ID is retained for answers and revisions, never the provider's
global “latest session.” **Reset agent session** preserves task history and files
but starts fresh agent context. Record useful output paths on the task so a new
session can find them. Agent-created files and worktrees are backed up and cleaned
up through the agent or your normal repository tools.

Raw provider logs are in `<data-dir>/logs/<run-id>.jsonl`; task state and events
are in `<data-dir>/board.sqlite3`.

The agent gets four task-scoped MCP tools:

```text
get_task()
add_comment(text)
request_input(question)
submit_for_review(summary, output_url="", kind="text")
```

The short reporting contract is in `acp/workflow.md`. ACP does not prescribe planning, implementation, Git, or testing steps. It asks the agent to report a blocking question or its result and actual verification, then end the turn for your response. Desktop prompts use equivalent run-scoped CLI commands instead of MCP instructions.

For a task linked to Linear, include evidence in an ACP comment or review summary:

```markdown
![Verified screen](</absolute/path/screenshot.png>)
[Verification report](</absolute/path/report.md>)
```

ACP saves a copy of each explicitly linked local file in its database, uploads it
to Linear's private file storage, and replaces the local destination in the mirrored
comment. Images remain inline and documents become downloadable links. A primary
`kind="file"` review output is included automatically. `file://` links also work.
Files must be readable at first submission, with a 20 MiB limit per file and 50 MiB
per comment. Missing or oversized files reject the report before moving the task
to review. Only link intended evidence; ACP does not inspect file contents for secrets.

Uploads use the existing delivery retries and reuse successful uploads across
restarts. Failed uploads remain visible in delivery errors and do not publish a
comment containing broken local links. Existing HTTP(S) links, code examples, and
source links ending in `:line` or `#Lline` are preserved. Plain paths, relative links,
reference-style Markdown, and attachments appearing only in the agent's chat are
not uploaded. Previously saved comments are not backfilled; resubmit evidence in
a new comment. Install the updated ACP package and restart its service to enable
this behavior for the running desktop bridge.

To produce a PR, instruct the task to create/update a GitHub PR and submit its canonical URL with `kind="pr"`. Your local Git and `gh` credentials must already permit that work. ACP itself does not independently create or merge PRs. A subsequent change-request run receives the retained session, task history, and new review feedback.

### Runtime boundaries

This is a **trusted local development tool**, not a security sandbox. A Git worktree isolates checkouts, not machine access. Claude is allowed its normal file tools, Bash, and the four task tools. Codex uses its workspace-write sandbox with network access and writable Git metadata for its launch repository. Review code and instructions in repositories before running an agent against them. Never give this unauthenticated board untrusted users or expose its API publicly.

The Codex adapter explicitly approves only the four task-scoped ACP MCP
tools so they work in headless mode. Its general approval policy remains `never`;
commands needing sandbox escalation fail rather than prompting an absent user.
The installed Codex CLI must support per-tool MCP approval configuration.

By default only one run executes at a time. `MAX_CONCURRENT_RUNS` increases this across tasks; there is still at most one active run per task. `RUN_TIMEOUT_SECONDS` bounds a run (default 30 minutes). In CLI mode, stopping the server or a task terminates its process group. A small non-agent process guard also terminates that group if the owning server dies unexpectedly.

## Headless usage

With the service running, use the installed `acp` command in
another terminal:

```bash
acp create "Add a health endpoint" \
  --description "Return HTTP 200 with JSON; add tests; submit a PR." \
  --provider codex --repo /path/to/repo --state todo

acp list
acp show TASK_ID
acp move TASK_ID todo
acp move TASK_ID waiting_on_input --comment "Which API version should we target?"
acp answer TASK_ID "Use v2." --blocker-id BLOCKER_COMMENT_ID
acp changes TASK_ID "Cover the error case in the test suite."
acp comment TASK_ID "Extra context for the agent."
acp cancel TASK_ID
acp reset-session TASK_ID
acp events --follow
```

Any new human comment on a task in Waiting on Input queues a follow-up for the agent to evaluate, whether it comes from the board, CLI, API, or Linear. No reply target or `/resume` command is required. Delayed Linear comments posted before the current blocking question are retained as context without waking the agent. The task moves to In Progress when the agent starts. Multiple comments waiting for the agent are combined into one queued run. `answer` remains a shortcut for replying to the blocker; `changes` queues a run for Ready for Review. Edits, agent progress updates, bot comments, integration echoes, and approval events never wake an agent on their own. New human comments on In Progress desktop tasks steer the existing chat; on Ready for Review they queue a continuation. Todo comments join queued context. Planning, Deploy, and Done comments do not start work.

The same operations are available through `TaskService` functions and HTTP. Main routes:

| Method / route | Purpose |
| --- | --- |
| `POST /api/tasks` | Create a task in Planning or Todo. |
| `GET /api/tasks` / `GET /api/tasks/{id}` | Board and complete task history. |
| `GET /api/activity` | Combined Codex and Claude agent counts, time-weighted concurrency averages, and per-source detail under `sources`. |
| `PATCH /api/tasks/{id}` | Edit context/provider/repository, subject to active-run checks. |
| `POST /api/tasks/{id}/transition` | Validate and commit a state change. |
| `POST /api/tasks/{id}/comments` | Comment; human input steers active app chats or resumes waiting/review tasks. |
| `PATCH` / `DELETE /api/tasks/{id}/comments/{comment_id}` | Comment edits/deletion also emit events. |
| `POST /api/tasks/{id}/request-input` | Atomically save the question and enter Waiting. |
| `POST /api/tasks/{id}/submit-for-review` | Save output/summary and enter Review. |
| `POST /api/tasks/{id}/request-changes` | Save feedback and queue a review iteration. |
| `POST /api/tasks/{id}/cancel` | Unqueue or cancel the run; stop app turns in Codex. |
| `POST /api/tasks/{id}/reset-session` | Clear the retained session while idle. |
| `GET /api/events` / `GET /api/events/stream` | Replayable JSON / live SSE events. |
| `POST /api/subscriptions` | Register an event webhook. |
| `GET /api/deliveries` | Inspect delivery status/errors. |
| `POST /api/deliveries/{id}/retry` | Manually retry a failed delivery. |

An optional `expected_version` on transition/update rejects stale UI or client writes with HTTP 409. Agent tool calls carry a run ID; calls from cancelled, superseded, or terminal runs are rejected. This is workflow fencing, not a user permissions system.

Example atomic blocker:

```bash
curl -sS http://127.0.0.1:8765/api/tasks/TASK_ID/request-input \
  -H 'Content-Type: application/json' \
  -d '{"question":"Which customer environment should this target?"}'
```

## Events and subscriptions

Tasks, comments, runs, workspaces, PR events, and integration errors publish to a SQLite event log. A task update, its events, and any delivery/run jobs commit together. There is no Kafka or separate broker.

```bash
curl -sS http://127.0.0.1:8765/api/subscriptions \
  -H 'Content-Type: application/json' \
  -d '{"url":"http://127.0.0.1:9000/events", "types":["task.state_changed","comment.*"], "secret":"your-shared-secret"}'

curl -N http://127.0.0.1:8765/api/events/stream
curl -sS 'http://127.0.0.1:8765/api/events?after=100&limit=200'
```

Patterns use shell-style matching: `comment.*`, `run.*`, or `*`. Each delivery includes the event's stable `id`, `schema_version`, `type`, `task_id`, `origin`, `data`, and `created_at`. Subscribers should deduplicate by event ID and use a task-state event's `data.version` to ignore stale updates. SSE supports `Last-Event-ID` and the `after` query parameter.

Delivery is **at least once**, not exactly once. Transient HTTP failures retry with jittered exponential backoff and honor Retry-After, up to eight attempts, then become visible failed deliveries. Permanent HTTP failures fail immediately. Completed deliveries cannot be manually replayed; retrying an already pending delivery is a no-op. A retry can arrive after a later event. Consumers must not assume global ordering. With a subscription secret, verify the raw-body HMAC-SHA256 in `X-ACP-Signature` (hex digest with `sha256=` prefix). `X-ACP-Delivery` identifies the outbox delivery.

Deleting a subscription prevents its undelivered jobs from sending. All event history and logs are retained in v0.1; there is no automatic retention cleanup.

## Linear setup in the app

Open **Settings → Linear**. Paste a personal API key (or save
`LINEAR_API_KEY` in `~/.local/share/acp/config.env`), then click **Continue**.
Choose a team and the agent for labeled issues: Codex, Claude Code, or Demo.
ACP discovers your account and matches statuses automatically. If statuses are
missing, their names are shown before you connect. Click **Connect Linear**. ACP creates the
`acp` label if it does not exist. No restart or hand-written status JSON is needed.

The seven lanes need distinct Linear statuses. Status names may differ; choose
existing statuses under **Customize status mapping**. Once issues are linked, the team and mapping
are fixed for that board to preserve their meaning. Use a separate `ACP_DATA_DIR`
for a different workspace/team.

**Assign a Linear issue to yourself and add the `acp` label to put it on your ACP board.**
The board owner is discovered from the API key and shown in the app. A different
user must use a separate ACP data directory; changing keys cannot silently transfer
board ownership. Existing
labeled issues are discovered automatically. Unlabeled issues are ignored.
Removing the label or assigning the issue to someone else cancels ACP runs and hides the card; stop any active desktop turn in Codex. History, sessions,
and worktrees remain. Re-adding it restores the same task. Archived issues,
issues moved outside the selected team, and unmapped statuses (such as Canceled)
are also removed from the active board. Todo and In Progress queue execution;
Planning leaves the issue ready for inspection.

ACP scans the owner’s labeled issues in the selected team, their statuses, and new comments every
minute, including after restart. **Sync now** requests an immediate
scan. This works without a public URL or tunnel. Setup shows the selected team,
agent, and any sync errors. Only comments created after ACP begins watching the
issue are eligible to trigger work; edits and bot comments never trigger runs.
Comment deletion synchronization requires webhooks. Local pending writes take
precedence over incoming status snapshots.

Within a labeled issue, use:

```text
/wait Which environment should this target?
/resume Target the staging environment.
/changes Add coverage for the expired-token case.
```

Any new human comment on a waiting or review issue resumes the task, including top-level comments and replies in other threads. Comments on an active Codex app task steer its existing chat.
`/resume` applies while Waiting; `/changes` applies while Ready for Review.
A silent drag into Waiting is rejected and corrected because ACP requires an
explicit blocking question. Move an issue to Done only after reviewing its output.

### Automatic repository selection

Leave the task's repository blank to let its selected Codex or Claude agent choose
from local Git checkouts using the issue and replies. ACP searches `~/Documents`,
`~/Projects`, `~/Developer`, `~/code`, and `~/src` (up to four levels, excluding
hidden/dependency directories). Set `REPO_SEARCH_ROOTS` to a colon-separated list
of parent directories to change the search on macOS/Linux. An explicit task
repository or `DEFAULT_REPO` takes precedence.

The discovery pass receives repository names, paths, and sanitized remote URLs.
Codex uses a read-only discovery sandbox; Claude has no tools in this pass.
The selected path must belong to the discovered catalog. Ambiguity asks for input;
no arbitrary repository is selected. The selected agent manages the implementation workspace, with the same retained session for revisions.

### Agent and CLI setup

The same setup is available to Codex, Claude, and other local agents as JSON-returning
commands. Save the key in `config.env`, or provide `--key-file /private/path/key`
with `discover` and `configure`; do not put keys directly in shell arguments.

```sh
acp start
acp linear discover
acp linear configure --team-id TEAM_ID --create-missing --provider codex
acp linear status
acp linear sync
```

`discover` returns teams, available statuses, suggested mappings, and missing lanes.
Omit `--create-missing` to require existing statuses. For custom mappings, pass
`--states-file mapping.json`. Saving activates automatic synchronization of issues
carrying the `acp` label. An agent should inspect the team before selecting it;
setting Todo can start real work. The key identifies the board owner; sync includes
only issues assigned to that user. API routes under `/api/integrations/linear` expose
the same operations. The task-scoped MCP tools remain limited to their own task.

ACP posts updates for each issue in one Linear comment thread. The first update
starts the thread; later updates reply to its root, including after a restart or
delivery retry. Existing issues reuse their earliest available ACP thread without
moving older comments. While a task is waiting for input, any new human comment
on the issue queues a follow-up for evaluation.

App-managed Linear settings are stored atomically with mode `0600` in
`ACP_DATA_DIR/linear.json` (by default `~/.local/share/acp/data/linear.json`). They
apply immediately and take precedence over legacy `LINEAR_*` values. API keys and
webhook secrets are never returned by setup/status responses. Existing manual
environment configurations continue to work; save through the app to enable
label-based synchronization.

### Optional instant updates

For faster incoming updates, run `acp hooks` and forward **port 8766 only** through
an HTTPS tunnel. In **Settings → Advanced → Instant updates**, enter
`https://your-forwarding-host/hooks/linear` and click **Register webhook**. ACP registers Issue and Comment events and
saves the signing secret automatically. A workspace admin key is required.
Agents can do the same with:

```sh
acp linear webhook --url https://your-forwarding-host/hooks/linear
```

The listener and tunnel must remain running for instant delivery; minute-by-minute
sync continues without them. A registered webhook is not proof of delivery: the
app separately displays the last accepted signed delivery. To change tunnel URLs,
remove the old ACP webhook in Linear, then register the new address. A stable
forwarding address avoids this step. The board/API port must stay private.

## Signed incoming webhooks

Keep the board on localhost. For GitHub/Linear's inbound HTTP calls, run the separate **hooks-only** listener in another terminal using the same configuration and data directory:

```bash
acp hooks
# Binds 127.0.0.1:8766. It exposes only /hooks/github and /hooks/linear.
```

Route an HTTPS webhook forwarding endpoint to **port 8766 only**, not the board/API port. The hooks-only app contains no task API, UI, or Swagger docs. It validates signatures before storing jobs; the main ACP service consumes them. No forwarding service or external webhook is created automatically.

### GitHub

Set `GITHUB_WEBHOOK_SECRET`; configure the same secret in a GitHub repository webhook. Subscribe to **Pull requests** and **Pull request reviews**. Its destination is the public HTTPS forwarding URL plus `/hooks/github`.

Set `GITHUB_TOKEN` with read access when linked PRs are private, so the backend can fetch the entire submitted review's inline comments. Git pushing and `gh` use their own existing local authentication.

Only PRs explicitly attached to a task with `kind="pr"` are handled. A submitted `changes_requested` review queues the task with the review body, inline file comments, diff context, and reviewed commit ID. Duplicate deliveries/reviews are deduplicated. Approval and ordinary review comments don't trigger runs. Feedback arriving during another run is queued; feedback during an unrelated blocker is retained without treating it as an answer.

A merged PR moves a task from Ready for Review to Deploy. **Merging is not deployment success.** Deploy-to-Done is manual or may be performed by your own subscriber after verifying deployment. No deployment command is implemented or run by default.

### Linear

Configure Issue and Comment webhooks to the forwarding URL plus `/hooks/linear`, with the same `LINEAR_WEBHOOK_SECRET`. Raw-body HMAC signatures, workspace identity, delivery IDs, and webhook timestamps are checked. Timestamps older than 60 seconds are rejected; synchronize the host clock. Duplicate delivery IDs do not enqueue duplicate work.

## Design and failure handling

```text
Browser / CLI / HTTP        Linear / GitHub webhooks
          \                       /
           task functions + validation
                      |
          SQLite transaction: task + events + jobs
                      |
              one local worker
               /             \
      agent run slots         durable deliveries
          |                       |
 Claude/Codex CLI + MCP       Linear / subscribers
          |
 retained session + agent-managed workspace
```

The worker starts agents and routes events; it never asks a separate agent to classify comments. The agent reasons about the work. Code validates transitions, records events, queues execution, and fences stale callbacks.

A database uniqueness constraint prevents concurrent live runs on the same task; follow-up inputs coalesce into one pending run. A process lock prevents two app workers against the same database. Do not run Uvicorn with multiple workers or live-reload for this app.

After a restart, active Codex desktop runs retain their identities and resume observation. Interrupted CLI runs become Waiting on Input with an explanation. Queued work is retained. Interrupted code-writing runs do **not** automatically retry: inspect the agent chat and files, then move to Todo or answer the blocker. Delivery jobs, unlike agent runs, are safe to retry with stable deduplication IDs.

Back up the data directory with the server stopped (SQLite WAL files matter for live copies), plus any worktrees and provider session storage you need. Secrets are not included in this source package; runtime `.env` files, database contents, logs, and code outputs can contain sensitive information. No automatic backup, migrations between future schema versions, worktree cleanup, or multi-host execution is included.

## Development and verification

```bash
make setup
make check
```

`make check` needs Node.js on PATH for the JavaScript syntax check; the app itself
has no Node runtime or build requirement. It runs the full Python suite, checks
Python syntax/undefined-name errors with Ruff, and checks JavaScript syntax.
GitHub Actions runs the same commands on macOS and Linux with Python 3.11 and 3.14.

`pyproject.toml` declares dependencies and development groups; `uv.lock` records
the resolved versions. Keep both files and `.python-version` in Git. Add runtime
dependencies with `uv add PACKAGE`, add development tools with
`uv add --dev PACKAGE`, and update dependencies deliberately with
`uv lock --upgrade` followed by `make setup` and `make check`. Normal setup and
run commands use `--locked` so an outdated lockfile fails visibly.

The test suite covers core invariants, concurrent scheduling, transactional event delivery, interruption recovery, signed webhooks, Linear synchronization, PR review handling, and real HTTP/subprocess/Git lifecycles using fake provider executables. A native MCP stdio test runs when the SDK is installed.

For optional browser tests:

```bash
uv sync --locked --group browser
uv run --locked --group browser playwright install chromium
uv run --locked --group browser python tests/browser_smoke.py
uv run --locked --group browser python tests/browser_overlay.py
# Or point to an installed Chromium:
CHROMIUM_EXECUTABLE=/usr/bin/chromium uv run --locked --group browser python tests/browser_smoke.py
```

The browser harness also has an explicitly labeled offline transport mode for
restricted environments. See **docs/VERIFICATION.md** for current results and
which live integrations were exercised.

### Code map

| File | Responsibility |
| --- | --- |
| `service.py` | Task/comment/state functions and atomic workflow rules. |
| `store.py` | SQLite schema, event log, durable outbox. |
| `worker.py` | Run scheduling and retryable event delivery. |
| `runners.py` | Claude/Codex CLI adapters, sessions, launch directories, logs. |
| `codex_activity.py` | Global Codex hook installation and observed activity intervals. |
| `claude_activity.py` | Global Claude Code hook installation, observed intervals, and the combined summary. |
| `activity.py` | Legacy ACP-run activity accounting. |
| `desktop.py` | Supported Codex app queue, dispatch lifecycle, run-scoped actions. |
| `mcp_server.py` | Four task tools exposed over stdio. |
| `integrations.py` | Single-workspace Linear sync and GitHub review events. |
| `api.py` | HTTP API, SSE, UI, signed webhook listener. |
| `static/` | Plain HTML/CSS/JavaScript UI. |
| `workflow.md` | Short task reporting contract for CLI agents. |

## Official references used

This is an independent implementation, not a fork of Symphony.

- Symphony's specification: https://github.com/openai/symphony/blob/main/SPEC.md
- Codex noninteractive execution: https://developers.openai.com/codex/noninteractive/
- Codex CLI reference: https://developers.openai.com/codex/cli/reference/
- Claude Code headless execution: https://code.claude.com/docs/en/headless
- Python MCP SDK (v1 interface): https://github.com/modelcontextprotocol/python-sdk/tree/v1.26.0
- Linear GraphQL: https://linear.app/developers/graphql
- Linear webhook validation: https://linear.app/developers/webhooks
- GitHub webhook events: https://docs.github.com/en/webhooks/webhook-events-and-payloads

Provider CLI behavior can change. The native adapters need a real authenticated smoke test on the machine where you will run them; the included demo is not proof of provider integration.
