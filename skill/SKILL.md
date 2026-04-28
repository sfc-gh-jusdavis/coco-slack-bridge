---
name: slack-bridge
description: "Bidirectional Slack DM bridge for remote notifications, confirmations, project switching, and SnowBoard task management. Use when: user says enable slack, start slack, activate slack, slack on, /slack, disable slack, stop slack, slack off, pause slack, pause, brb, resume slack, resume, unpause, back, I'm back. Also use for: sending notifications to user's phone, requesting remote approval, checking for Slack replies, handling /projects /use /task /new slash commands from the inbox. Triggers: enable slack, start slack, activate slack, slack on, /slack, disable slack, stop slack, deactivate slack, slack off, pause slack, pause, brb, take a break, hold on, resume slack, resume, unpause, back, I'm back, notify, bridge, remote, phone, DM, confirm remotely, slack task, slack project."
tools: ["bash", "cron_create", "cron_delete", "cron_list", "snowboard_create_task", "snowboard_update_task"]
---

# Slack Bridge

Bidirectional Slack DM bridge — send notifications, request confirmations with Approve/Deny buttons, switch projects, create/manage SnowBoard tasks, and receive replies from the user's phone.

**This bridge is opt-in per session.** The bot process runs in the background (started by the SessionStart hook), but Slack interaction is only activated when the user explicitly asks.

## Bridge Paths

Use these paths (the bridge is installed at `~/Apps/coco-slack-bridge`):

```
BRIDGE=~/Apps/coco-slack-bridge/bin/coco-bridge
# Resolve session ID (Desktop sets CORTEX_SESSION_ID; CLI uses a project-specific file)
PROJECT_NAME=$(basename "$PWD")
if [ -n "${CORTEX_SESSION_ID:-}" ]; then
  SID="$CORTEX_SESSION_ID"
elif [ -f "$HOME/.cortex-slack-bridge/session_${PROJECT_NAME}.id" ]; then
  SID=$(cat "$HOME/.cortex-slack-bridge/session_${PROJECT_NAME}.id")
else
  SID="default"
fi
INBOX=~/.cortex-slack-bridge/inbox_${SID}.json
WAKE=~/.cortex-slack-bridge/wake_${SID}
```

## SessionStart Prompt

When the SessionStart hook fires, it outputs a message telling you to ask the user if they want Slack enabled. When you see this, use `ask_user_question` with Yes/No:

- Question: "Enable Slack notifications for this session?"
- Options: "Yes" / "No"

If "Yes", register the project and run the Enable flow. If "No", do nothing.

## Enabling Slack for This Session

**MANDATORY: You MUST call `cron_create` before sending the activation message. Without the cron, messages arrive but are never read back.**

1. Register this project (so `/projects` and `/use` in Slack see it):
```bash
~/Apps/coco-slack-bridge/bin/coco-bridge project register "$(basename "$PWD")" "$PWD"
```

2. Create the inbox polling cron (fast 5s loop within each minute):
```
cron_create with cron "*/1 * * * *" and prompt:
"Slack inbox fast check: You MUST run the following bash loop FIRST. The loop polls the inbox every 5 seconds for up to 55 seconds within this 1-minute cron fire. As soon as entries appear, process them and exit the loop early.

PROJECT_NAME=$(basename "$PWD")
if [ -n "${CORTEX_SESSION_ID:-}" ]; then
  SID="$CORTEX_SESSION_ID"
elif [ -f "$HOME/.cortex-slack-bridge/session_${PROJECT_NAME}.id" ]; then
  SID=$(cat "$HOME/.cortex-slack-bridge/session_${PROJECT_NAME}.id")
else
  SID="default"
fi
INBOX=~/.cortex-slack-bridge/inbox_${SID}.json
WAKE=~/.cortex-slack-bridge/wake_${SID}
for i in $(seq 1 11); do
  if [ -f \"$INBOX\" ]; then
    content=$(cat \"$INBOX\")
    if [ \"$content\" != \"[]\" ] && [ -n \"$content\" ]; then
      echo \"INBOX_HIT_$i\"; echo \"$content\"; break
    fi
  fi
  sleep 5
done

If the loop printed INBOX_HIT_*, process the entries per the rules below (reply / command / confirmation). After processing, clear the inbox:
  echo '[]' > $INBOX
  rm -f $WAKE

If no hit after all 11 iterations, stay completely silent (no output, no tool calls beyond the loop)."
```

3. Send the activation message:
```bash
~/Apps/coco-slack-bridge/bin/coco-bridge send "Slack bridge active for project $(basename "$PWD")"
```

4. Confirm to the user: "Slack bridge enabled."

## Inbox Entry Types

Every entry has `type`. Process each one in order, then clear the inbox.

### `reply` — free-text DM

Treat `text` as user input in this session. Respond as you normally would, AND echo a concise 2-3 sentence summary back to Slack via `coco-bridge send --thread "..."` (always use `--thread` so the response lands in the correct Slack thread).

If the entry has a `thread_ts` field, the message came from a specific Slack thread. The bridge has already routed it to the correct session based on the thread-session registry.

### `command` — structured slash-command from Slack

Dispatch by `command` field. The entry's `project` metadata should match this session; if not, warn in Slack and do nothing else.

| `command` | Action |
|---|---|
| `snowboard.create_task` | Call `snowboard_create_task` with args `{title, description, priority}`. tag="slack", link set to something stable (e.g. `slack://session/<id>`). Reply in Slack with the returned task id. |
| `snowboard.list_tasks` | There is no direct list tool — read the user's taskboard via `cat` or memory, then summarize any tasks matching the optional `status` arg. Keep the reply under 15 lines. |
| `snowboard.update_task` | Call `snowboard_update_task` with `{taskId, status}` from `args`. Reply in Slack confirming. |
| `coco.new_session` | See "Starting a new CoCo CLI session" below. |

Always acknowledge in Slack with `coco-bridge send` after the action completes (success or failure).

### `confirmation` — Approve/Deny button click

Use `confirmation_id` to correlate with the in-flight `coco-bridge confirm` call. The `notify.send_confirmation` function pops these automatically.

## Starting a new CoCo CLI session (`/new`)

When a `command: coco.new_session` entry arrives:

1. Look up the target project path via `coco-bridge project list`.
2. If the project exists, open a new Terminal window and launch `cortex` in that directory with the optional prompt:
```bash
osascript -e "tell application \"Terminal\" to do script \"cd '<path>' && cortex '<prompt>'\""
```
3. Reply in Slack: "Started new CoCo session in `<project>` — it will auto-register once running."

If the project doesn't exist, reply with an error via Slack and the list of known projects.

## Project Guard

If an incoming `command` or `reply` has `project.name` set and it doesn't match `$(basename "$PWD")` (or the current registered alias), send a warning to Slack: ":warning: Command targeted project `X` but this session is in `Y`. Ignoring." Do NOT act on it.

## Pausing Slack

When the user says "pause", "brb", etc.:

1. Delete the inbox-check cron (`cron_list`, then `cron_delete`).
2. Create a slow heartbeat cron:
```
cron_create with cron "*/5 * * * *" and prompt:
"Slack pause heartbeat: resolve SID the same way as the fast check (CORTEX_SESSION_ID, else session_${PROJECT_NAME}.id, else default). cat ~/.cortex-slack-bridge/inbox_${SID}.json. If [] or missing, silent. If entries, look for resume keywords (resume, back, unpause, I'm back). If found, delete this heartbeat, recreate the fast inbox cron from the slack-bridge skill, send 'Slack bridge resumed', then process queued messages. Else leave entries queued, silent."
```
3. `coco-bridge send "Slack bridge paused. Say 'resume' to continue."`

## Resuming Slack

1. Delete any pause heartbeat cron.
2. Recreate the fast inbox-check cron (same as Enable step 2).
3. `coco-bridge send "Slack bridge resumed"`.

## Disabling Slack

1. Delete the inbox-check cron.
2. Resolve SID (same pattern as the cron), then: `echo '[]' > ~/.cortex-slack-bridge/inbox_${SID}.json`
3. `coco-bridge send "Slack bridge off"`

## Polling latency

Default config: 5s step, 11 steps per 1-minute cron = ~5s worst-case latency.

Optional wake-signal (set `poll_wake_enabled: true` in `~/.cortex-slack-bridge/config.json`): the bridge touches `~/.cortex-slack-bridge/wake_${SESSION}` when it writes to the inbox. The inbox loop above already checks for the wake file implicitly via the 5s poll — if you want sub-5s, run `fswatch -1 "$WAKE"` instead of `sleep 5`.

Tune the loop granularity with `coco-bridge set-poll-interval <seconds>` (2-30). Tighter polling = more agent wake-ups and tokens.

## Commands (wrapper quick reference)

```bash
BRIDGE=~/Apps/coco-slack-bridge/bin/coco-bridge

$BRIDGE send "Task done"               # plain DM
$BRIDGE send "Done" --type success     # color-coded
$BRIDGE confirm "Deploy?" --id deploy-1  # Approve/Deny buttons
$BRIDGE inbox                          # current inbox JSON
$BRIDGE history 50                     # last 50 audit events
$BRIDGE project list                   # list registered projects
$BRIDGE project use <name>             # set active project
$BRIDGE task add high "Title" "Desc"   # local mirror of /task add
$BRIDGE set-poll-interval 5            # adjust polling cadence
```

## When to Notify / Confirm / Ask via Slack

- Long-running task completes or fails -> `coco-bridge send` with `--type success|error`.
- Destructive operation (DROP, DELETE, prod deploy) -> `coco-bridge confirm`.
- Blocking question when user isn't at terminal -> `coco-bridge send` plain-text with numbered options.
- Don't use `ask_user_question` while Slack is active — it blocks on the CLI.

## Responding to Slack Messages

Whenever you process a `reply` or `command` entry from the inbox, *always* send a concise response back via `coco-bridge send --thread`. The user is on their phone — 2-3 sentences max, with full detail in the CLI.

## Thread-per-Agent Routing

The bridge supports mapping Slack threads to specific CoCo sessions. When a thread is mapped, all replies in that thread are routed to the mapped session's inbox instead of the active session.

**How it works:**
- Top-level DMs (no `thread_ts`) route to the active session (unchanged behavior).
- Threaded replies (has `thread_ts`) check the thread-session registry first. If a mapping exists, the message goes to that session. Otherwise, falls back to the active session.
- The `!threads` command lists all active thread-session mappings from Slack.

**Registering a thread mapping:**
To map the current session to a Slack thread (so future replies in that thread come here), use:
```python
# From Python (bridge-side):
from cortex_slack_bridge.config import register_thread_session
register_thread_session(thread_ts="1234567890.123456", session_id="abc-123", project_name="my_project")
```

The `!new` command flow can auto-register: when a new session is spawned from Slack, the bridge maps the originating thread to the new session.

## Plan Approval via Slack

When you enter plan mode (`enter_plan_mode`) and the Slack bridge is active, use this flow instead of relying solely on the CLI for plan confirmation:

1. **Draft the plan** as normal (explore codebase, design approach).
2. **Send a condensed plan summary to Slack** using the confirm command:
```bash
~/Apps/coco-slack-bridge/bin/coco-bridge confirm "Plan: <condensed summary — key steps, files to change, max 10 lines>" --id "plan-$(date +%s)" --timeout 300
```
3. **The command blocks** until the user taps Approve or Deny in Slack (5 min timeout).
4. **Read the result** from stdout:
   - `approved` → Call `exit_plan_mode` with the full plan and proceed to implement.
   - `denied` → Send a follow-up to Slack asking what to change: `coco-bridge send --thread "Plan denied. What should I change?"`. Wait for the reply via the inbox, revise, and re-confirm.
   - `timeout` (exit code 1) → Fall back to CLI confirmation via `exit_plan_mode` as normal.

**Plan summary formatting for Slack** (the user is on their phone — keep it scannable):
- Start with a one-line title: `*Plan: <feature name>*`
- List 3-6 key implementation steps as bullet points
- List the files to be changed
- End with `Full plan details available in CLI.`
- Total: 10 lines max. Don't include code snippets or full file paths — just file names.

Example:
```
*Plan: Add caching to API layer*
• Add Redis connection helper to config
• Wrap fetch_user and fetch_orders with cache decorator
• Add cache invalidation on write operations
• Files: config.py, api/users.py, api/orders.py
Full plan details available in CLI.
```

## Proactive Updates on Session Resume

When a session resumes from a context summary and the summary indicates pending work the user was waiting on: finish the work, then immediately `coco-bridge send` the result. Don't wait to be asked.

## Inbox Format (reference)

```json
[
  {
    "type": "reply",
    "text": "user's message",
    "user": "U02M8RTD1HT",
    "ts": "1234567890.123456",
    "thread_ts": "1234567890.000001",
    "received_at": 1234567890.123,
    "project": {"name": "slack_v1", "path": "/abs/path"}
  },
  {
    "type": "command",
    "command": "snowboard.create_task",
    "args": {"title": "...", "description": "...", "priority": "high"},
    "user": "U...",
    "ts": "...",
    "thread_ts": null,
    "received_at": 1234567890.0,
    "project": {"name": "slack_v1", "path": "/abs/path"}
  },
  {
    "type": "confirmation",
    "confirmation_id": "deploy-123",
    "response": "approved",
    "user": "U...",
    "received_at": 1234567890.0,
    "project": {"name": "slack_v1", "path": "/abs/path"}
  }
]
```

Note: `thread_ts` is present when the message was sent from within a Slack thread. It is `null` for top-level DMs. The bridge uses this to route threaded replies to the correct session.
