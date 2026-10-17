# Codex desktop dispatcher implementation plan

Goal: run ACP Codex tasks automatically in desktop-owned chats through the verified
`codex queue` command and one user-connected dispatcher chat. Implement inline.

Design: ACP retains scheduling, run guards and worktrees. An app dispatcher claims
a run once, chooses a repository when necessary, creates or continues its app chat,
waits using app tools, and acknowledges actual completion. Task agents use a narrow
run-scoped CLI instead of configuring global MCP credentials or untrusted hooks.
Settings exposes connection instructions and the dispatcher ID. Install/setup ships
these instructions; connecting is one agent-assisted action after installation.

Constraints: supported queue and app tools only; no IPC, UI automation, trust bypass,
or silent CLI fallback after connection. Preserve existing CLI sessions, worktrees,
Linear ownership and user configuration. Desktop cancellation revokes ACP callbacks;
stop the actual turn in Codex. Unsettled app executions block overlapping retries.

Verification focus: duplicate dispatch; wrong/stale session callbacks; cancellation
and restart before acknowledgement; app closed/no start; existing CLI session switch;
follow-up retains chat/worktree; no desktop routes on public webhook listener.

- [x] Add failing lifecycle/route/runner tests for connection, dispatch claims,
      scoped actions, stale callbacks, completion, and unresolved previous runs.
- [x] Implement desktop service, queue runner, CLI and local API integration.
- [x] Add simple Settings connection instructions and desktop execution visibility.
- [x] Run focused tests, full checks, then actual app create/input/review/revision flow.
- [x] Review final diff, document real evidence and operational limits.

Review: three important recovery findings fixed with failing-then-passing tests:
cancel/restart before binding; failure before resumed prompt delivery; claim/cancel
transaction race. No review findings deferred. Additional live verification fixed
permission-escalation guidance and renamed turn completion to acknowledgement.
Ruling: use a dispatcher coordination turn on each run, including follow-ups, so
it can observe actual completion without requiring global hooks or hook trust.
Cost: a small additional model turn on follow-ups; documented in README.

Additional concurrency regression: serialize Codex app runs at claim time while
allowing other providers into extra worker slots. This prevents later app jobs
from timing out behind a busy dispatcher.

Follow-up requested by the user: delegate worktrees and working directories to
agents and simplify task prompts. The retained ACP-worktree design above is
superseded. Removed Git mutation from launch/preparation, kept old launch paths
for existing CLI sessions, and separated desktop command instructions from the
CLI MCP reporting contract. New behavioral regressions first failed on ACP
worktree creation, then passed with delegated workspace ownership.
