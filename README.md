# ACP

A small, local-first Kanban for **one person**, **Claude Code / Codex**, and optionally **one Linear workspace**. One Python backend, SQLite, and a browser UI with no build step. No accounts, roles, separate manager agent, Redis, or microservices.

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
acp stop           # Stop the server and interrupt active agent runs cleanly
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
active runs and retains their worktrees for explicit retry. Closing a browser tab
does not stop tasks. Interactive API documentation is at the reported URL plus
`/docs`. Choose **Try a demo** to exercise input and review without an agent account.

### Storage and configuration

By default ACP uses `$XDG_DATA_HOME/acp`, or `$HOME/.local/share/acp`:

- `config.env`: provider commands, defaults, and optional integration settings.
- `data/`: SQLite database, task worktrees, run logs and private service metadata.
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
curl -fsSL https://github.com/venti-josh/agent-control-plane/releases/download/v0.1.0/install.sh | sh
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
Keep `.env`, `.acp`, and `.venv` out of Git. Real tasks need a Git repository with
at least one commit; install and authenticate Codex or Claude separately.

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

After committing and pushing the source, push a matching `vX.Y.Z` tag. The Release
workflow runs checks and the installed demo flow on macOS and Linux before creating
a GitHub Release. No package is published to PyPI. Local asset tests do not establish
that a GitHub release has been published or downloaded successfully.

## What is implemented

| Lane | Behavior |
| --- | --- |
| Planning | Define or edit a task. No run starts. |
| Todo | Atomically queue a run. Remains Todo until a process actually starts. |
| In Progress | The runner owns this state. The task's own agent plans and implements. |
| Waiting on Input | A nonempty blocking comment is required in the same database transaction. The agent process stops. |
| Ready for Review | Output and summary are saved; the process stops. Explicit changes-requested feedback queues another run. |
| Deploy | Deployment handoff. A merged linked PR moves here when the task was awaiting review. |
| Done | Explicit completion. Nothing is automatically merged, deployed, or marked done. |

The seven lanes are task states, not process states. Runs separately record queued, running, succeeded, failed, cancelled, or interrupted. A successfully paused run can appear under Waiting on Input: it successfully asked a question, not completed the task.

The UI supports drag-and-drop, a keyboard-accessible lane selector, comments, answer-and-resume, change requests, outputs, run logs, task edits, stop/unqueue, session reset, event subscriptions, and Linear import. State/comment changes publish persisted events; the UI receives server-sent events.

## Use a real agent

Install and authenticate the **Claude Code CLI** or **Codex CLI** on the same machine and OS user that runs ACP. Verify the CLI works in your terminal first. ACP uses that CLI's authentication; it does not read ChatGPT connector credentials.

Set these in the `config.env` path printed by `acp setup` (or `.env` for source development), then restart ACP:

```dotenv
DEFAULT_PROVIDER=claude
DEFAULT_REPO=/absolute/path/to/your/git-repository
```

Use `codex` instead of `claude` to change the default. Each task can override the agent and repository. Model names are optional: `CLAUDE_MODEL` / `CODEX_MODEL`; otherwise the provider CLI chooses its configured default.

Create a task with acceptance criteria and put it into Todo. On its first real run, ACP creates a branch and worktree from your local repository's **current committed HEAD**. Uncommitted changes, ignored `.env` files, dependencies, and untracked files are **not copied**. Prepare the repository and its setup instructions accordingly.

```text
Your existing checkout:   /path/to/repo                   (not checked out by ACP)
Task branch:             acp/<task-prefix>
Task worktree:           <data-dir>/worktrees/<task-id>
Raw provider logs:       <data-dir>/logs/<run-id>.jsonl
Task state and events:   <data-dir>/board.sqlite3
```

The task's session ID and worktree are retained for answers and revisions. Resumption always uses that specific session, never the provider's global “latest session.” If a provider session becomes unavailable, **Reset agent session** preserves the worktree and passes the task history to a fresh session. If a worktree disappears, execution fails visibly rather than silently recreating it and losing work.

The agent gets four task-scoped MCP tools:

```text
get_task()
add_comment(text)
request_input(question)
submit_for_review(summary, output_url="", kind="text")
```

Instructions are in `acp/workflow.md`. There is no separate planning or supervisory model call. The task's existing agent inspects context, makes a plan, asks a question when necessary, and submits its work. Terminal tools end the turn; agents should not poll or stay alive awaiting your response.

To produce a PR, instruct the task to create/update a GitHub PR and submit its canonical URL with `kind="pr"`. Your local Git and `gh` credentials must already permit that work. ACP itself does not independently create or merge PRs. A subsequent change-request run receives the same branch, retained session, task history, and new review feedback.

### Runtime boundaries

This is a **trusted local development tool**, not a security sandbox. A Git worktree isolates checkouts, not machine access. Claude is allowed its normal file tools, Bash, and the four task tools. Codex uses its workspace-write sandbox with network access and writable Git metadata for its worktree. Review code and instructions in repositories before running an agent against them. Never give this unauthenticated board untrusted users or expose its API publicly.

The Codex adapter explicitly approves only the four task-scoped ACP MCP
tools so they work in headless mode. Its general approval policy remains `never`;
commands needing sandbox escalation fail rather than prompting an absent user.
The installed Codex CLI must support per-tool MCP approval configuration.

By default only one run executes at a time. `MAX_CONCURRENT_RUNS` increases this across tasks; there is still at most one active run per task. `RUN_TIMEOUT_SECONDS` bounds a run (default 30 minutes). Stopping the server or a task terminates its process group. A small non-agent process guard also terminates that group if the owning server dies unexpectedly.

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
acp answer TASK_ID "Use v2."
acp changes TASK_ID "Cover the error case in the test suite."
acp comment TASK_ID "Extra context, without starting a run."
acp cancel TASK_ID
acp reset-session TASK_ID
acp events --follow
```

Ordinary comments **do not** trigger execution. `answer` replies to the current blocker and queues a run. `changes` does the same for a task in Ready for Review. Edits, progress updates, integration echoes, and approval events never wake an agent on their own.

The same operations are available through `TaskService` functions and HTTP. Main routes:

| Method / route | Purpose |
| --- | --- |
| `POST /api/tasks` | Create a task in Planning or Todo. |
| `GET /api/tasks` / `GET /api/tasks/{id}` | Board and complete task history. |
| `PATCH /api/tasks/{id}` | Edit context/provider/repository, subject to active-run checks. |
| `POST /api/tasks/{id}/transition` | Validate and commit a state change. |
| `POST /api/tasks/{id}/comments` | Comment; optionally reply to a blocker and resume. |
| `PATCH` / `DELETE /api/tasks/{id}/comments/{comment_id}` | Comment edits/deletion also emit events. |
| `POST /api/tasks/{id}/request-input` | Atomically save the question and enter Waiting. |
| `POST /api/tasks/{id}/submit-for-review` | Save output/summary and enter Review. |
| `POST /api/tasks/{id}/request-changes` | Save feedback and queue a review iteration. |
| `POST /api/tasks/{id}/cancel` | Unqueue or request process termination. |
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

Delivery is **at least once**, not exactly once. HTTP failures retry with exponential backoff, up to eight attempts, then become visible failed deliveries. A retry can arrive after a later event. Consumers must not assume global ordering. With a subscription secret, verify the raw-body HMAC-SHA256 in `X-ACP-Signature` (hex digest with `sha256=` prefix). `X-ACP-Delivery` identifies the outbox delivery.

Deleting a subscription prevents its undelivered jobs from sending. All event history and logs are retained in v0.1; there is no automatic retention cleanup.

## Optional Linear integration

One workspace and one selected team are configured at a time. Linear workflow states are mapped through that team's seven distinct state IDs. The SQLite database is the canonical local task state; Linear is a synchronized interface. Only **explicitly imported issues** are tracked—there is no workspace-wide automatic execution.

Supply `LINEAR_API_KEY`, `LINEAR_WORKSPACE_ID`, `LINEAR_TEAM_ID`, `LINEAR_WEBHOOK_SECRET`, and the seven-entry `LINEAR_STATES_JSON` in `config.env` (or the explicit source `.env`). `.env.example` includes the exact mapping keys. Use your team's actual workflow state IDs, not names or status types. Configure the states in Linear before importing tasks. Restart after changing configuration.

A read-only GraphQL query to look up the workspace, teams, and states:

```graphql
query {
  organization { id name }
  teams {
    nodes {
      id
      name
      states { nodes { id name type } }
    }
  }
}
```

Use Linear's developer explorer or its GraphQL endpoint with your API key. State names may differ from the lane labels, but each of the seven lanes must map to a different state ID. The API validates the workspace/team of imported issues.

In the board, open **Integrations & events → Import issue**. Provide a specific issue such as `ENG-123`, choose the agent, and supply its repository. Importing Todo queues execution; importing In Progress queues it locally rather than inventing a running process. To import an already-waiting issue, use the API's `question` field so its blocking reason is explicit.

```bash
curl -sS http://127.0.0.1:8765/api/integrations/linear/import \
  -H 'Content-Type: application/json' \
  -d '{"issue_id":"ENG-123","provider":"claude","repo_path":"/path/to/repo"}'
```

Configure Linear **Issue** and **Comment** webhooks to the signed listener described below. Linked status/title/description changes and comments are synchronized. Local comments carry an invisible correlation marker; echoed integration updates do not start another run.

Within an imported Linear issue:

```text
/wait Which environment should this target?
/resume Target the staging environment.
/changes Add coverage for the expired-token case.
```

A human threaded reply to the current blocking comment also resumes. Plain comments, comment edits, and integration-authored comments do not trigger runs. `/resume` applies only while Waiting; `/changes` applies only while Ready for Review. An invalid `/wait` command is preserved as a comment with an explanation.

Linear cannot be prevented from accepting its own UI's silent drag into Waiting. ACP instead rejects that synchronized change, restores the canonical lane, and asks for `/wait <question>`. Your locally enforced invariant remains intact.

**Reconcile linked statuses** fetches current status/context snapshots for imported issues after downtime. It does not backfill missed comments or GitHub reviews. Use provider webhook redelivery or the explicit local answer/change-request command for missed feedback. There is no periodic external polling in v0.1. If local sync work is still pending, local state takes precedence over an incoming snapshot; this is not a general bidirectional merge engine.

To change workspaces, use another `ACP_DATA_DIR` and its matching configuration. Existing data is deliberately not silently reassigned across workspaces.

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
 retained session + per-task Git worktree
```

The worker starts agents and routes events; it never asks a separate agent to classify comments. The agent reasons about the work. Code validates transitions, records events, queues execution, and fences stale callbacks.

A database uniqueness constraint prevents concurrent live runs on the same task; follow-up inputs coalesce into one pending run. A process lock prevents two app workers against the same database. Do not run Uvicorn with multiple workers or live-reload for this app.

After a restart, interrupted runs become Waiting on Input with an explanation. Queued work is retained. Interrupted code-writing runs do **not** automatically retry: inspect the retained branch, then move to Todo or answer the blocker. Delivery jobs, unlike agent runs, are safe to retry with stable deduplication IDs.

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
| `runners.py` | Claude/Codex CLI adapters, sessions, worktrees, logs. |
| `mcp_server.py` | Four task tools exposed over stdio. |
| `integrations.py` | Single-workspace Linear sync and GitHub review events. |
| `api.py` | HTTP API, SSE, UI, signed webhook listener. |
| `static/` | Plain HTML/CSS/JavaScript UI. |
| `workflow.md` | Task-agent instructions, including its planning step. |

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
