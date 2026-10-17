# Codex app integration feasibility

Verified locally on 2026-10-17 with `codex-cli 0.162.0-alpha.17.2` and the
running ChatGPT desktop app's Codex surface. The probes below led to the local implementation described in README.
This integration is included in v0.1.2; v0.1.1 uses CLI execution.

## What worked

| Interface | Observed result | Implication for ACP |
| --- | --- | --- |
| `codex app-server` over stdio: `thread/start`, `turn/start` | Created a persistent thread and completed a prompt. | Supported protocol for a separate Codex client. |
| `thread/resume`, then `turn/start` | Continued the same thread and completed a second turn. | API-owned sessions can be retained across connections. |
| Desktop `read_thread` during the API turn | Read persisted history, but reported `notLoaded` and an interrupted turn while the API server was actively executing `sleep 45`. | Saved history visibility is not live desktop ownership. |
| `codex queue` targeting an idle desktop-owned chat | The app automatically started and completed a new turn, without a click. | Supported external wake-up for an existing connected app chat. |
| Queued instruction asking that app chat to create a new chat | The app chat called `create_thread`; its new chat completed its prompt. | A connected dispatcher chat can create task chats from queued ACP requests. |
| `codex queue` targeting that new child chat directly | Completed another turn in the same app chat, returning `ACP child continuation completed.` | Follow-ups do not need a dispatcher turn once the task chat exists. |
| `codex queue` targeting an unloaded API-owned thread after its server exited | Accepted the message, but the desktop did not execute it during the observation period. | Queue acceptance alone is not proof of app execution. |

The queue wake-up was asynchronous, approximately seven seconds in the desktop
probe. The command's successful exit must never be interpreted as task completion.
The app-owned dispatcher and child chats both reported `idle` with completed turns
after execution; the separate API-owned chat remained `notLoaded`.

## Supported integration shape

The verified route requires one app-owned dispatcher chat, connected during setup:

1. ACP queues a request using `codex queue --thread <dispatcher-id> --message ...`.
2. The dispatcher uses the app's thread tools to create a task chat, and records
   its ID against the ACP task.
3. Subsequent input is dispatched to the same task chat through the dispatcher.
4. Run-scoped callbacks report actual start and task actions. The dispatcher waits
   for the app turn to end and acknowledges completion. Queue acceptance alone
   is only delivery acknowledgement. No hooks are installed.

This route requires the desktop app to remain open and adds a dispatcher model
turn for every run, including follow-ups. The app's agent-facing `create_thread` tool
is not an external HTTP endpoint available to ACP. No private desktop IPC,
UI automation, authentication-file access, or client-identity spoofing was used.

## Hook installation boundaries

Codex documents user hooks in `~/.codex/hooks.json`, project hooks, and plugin
hooks. An installer can add ACP hooks while preserving other hooks. Codex requires
the user to review and trust each new or changed non-managed hook before it runs.
Installation must not write Codex's trust records or enable the trust-bypass flag.

`SessionStart`, `UserPromptSubmit`, `Stop`, and `Interrupt` can connect lifecycle
events to ACP. `Stop` can request continuation of the current chat. These callbacks
do not themselves create an initial app-owned chat or wake an idle chat when a
Linear issue arrives. Combining hooks with the verified queue/dispatcher route
is distinct from claiming hooks alone implement automatic app task creation.

Any implementation must scope hooks to ACP-bound chats, fence callbacks by run
and turn, preserve existing hooks, avoid duplicate dispatch on retries, and make
the app-closed, untrusted-hook, cancellation, and timeout cases visible.

## Probe evidence

- API-owned chat: `01a14a49-0b83-7de3-81cc-cde7bc58a53b`.
  Initial and resumed turns completed. Closing the probe's server after the
  resumed turn interrupted a further queued probe turn; this was probe teardown,
  not a successful desktop handoff.
- Desktop chat: `01a14a4b-4584-7c81-a4c2-a00b2d5c43fe`.
  Seed turn completed, then a CLI-queued turn returned
  `ACP desktop queue completed.` Its next queued turn created the child below.
- Child created by the queued dispatcher instruction:
  `01a14a4c-a426-7d20-92cb-e436247d2087`, returned `ACP child completed.`
  A direct CLI-queued follow-up completed in the same chat and returned
  `ACP child continuation completed.`
- Local ignored scratch evidence: `.acp/app-api-probe/`, including `events.jsonl`
  and `resume-events.jsonl`. The probe scripts are throwaway, not production adapters.

## Official references

- [App-server lifecycle](https://learn.chatgpt.com/docs/app-server#lifecycle-overview)
- [Codex queue command](https://learn.chatgpt.com/docs/developer-commands#codex-queue)
- [Hooks and trust requirements](https://learn.chatgpt.com/docs/hooks)

## Implementation outcome

The local candidate implements `acp desktop instructions`, connection management,
a supported queue runner, and run-scoped `claim`, `prepare`, `bind`, `started`,
`action`, and `acknowledge` commands. The dispatcher coordinates each run, including
follow-ups, so actual app completion can be observed without installing hooks.
This adds a small dispatcher turn to follow-ups as well as new tasks.

Cancellation revokes ACP callbacks; stopping the actual app turn remains a Codex
UI action. Unacknowledged app runs block overlapping retries. New app chats must
start within 120 seconds (or the configured run limit if shorter).

The candidate was installed locally from its wheel and connected to the real board
on port 8765. Existing configuration bytes, tasks, session IDs, comments, runs, and
artifacts were preserved. That verification used a local build before v0.1.2 publication.
See VERIFICATION.md for the subsequent public release and upgrade checks.


## Agent-owned workspaces

The subsequent simplification removes ACP worktree/branch creation from both
CLI and desktop execution. Codex app task agents use their normal workspace tools;
Claude and Codex CLI start from the selected repository and choose their own
workspace. Previously saved CLI workspace paths remain usable for existing
sessions, without branch enforcement. The task prompt supplies context and a
short reporting contract, without an implementation checklist or Git policy.
