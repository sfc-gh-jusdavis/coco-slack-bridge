# CoCo Slack Bridge

> Fork of [iamontheinet/cortex-code-cli-slack-bridge](https://github.com/iamontheinet/cortex-code-cli-slack-bridge) with project organization, SnowBoard task management, and low-latency (~5s) polling.

Bidirectional Slack DM bridge for [Cortex Code](https://docs.snowflake.com/en/user-guide/cortex-code/cortex-code-cli) (Desktop + CLI). Get notifications, switch between projects, create/manage SnowBoard tasks, approve/deny actions, and steer your agent from your phone.

## What's different from upstream

| Feature | Upstream | This fork |
|---|---|---|
| Slash commands in Slack | none | `/projects`, `/use`, `/task add\|list\|status`, `/new`, `/help` |
| Project tracking | n/a | `projects.json` registry with CWD auto-registration |
| Task management | n/a | SnowBoard integration via skill dispatcher |
| Polling latency | ~30s | ~5s (configurable 2-30s) |
| Wake-signal | n/a | Optional `wake_{session}` touch on inbox write |
| Session routing | active_session file | registry-first, with session metadata fallback |

Everything else from upstream (Socket Mode, Keychain, color-coded notifications, Approve/Deny buttons, audit history) still works.

## Quick start

```bash
git clone https://github.com/sfc-gh-jusdavis/coco-slack-bridge.git ~/Apps/coco-slack-bridge
cd ~/Apps/coco-slack-bridge
python3 -m venv .venv
.venv/bin/pip install -e .

# Store Slack tokens in macOS Keychain
bin/coco-bridge setup-keychain

# Install skill + SessionStart hook
mkdir -p ~/.snowflake/cortex/skills/slack-bridge
cp skill/SKILL.md ~/.snowflake/cortex/skills/slack-bridge/SKILL.md
cp demo-start-hook.sh ~/.cortex-slack-bridge/start-hook.sh
chmod +x ~/.cortex-slack-bridge/start-hook.sh
# Add a SessionStart hook entry in ~/.snowflake/cortex/hooks.json pointing at
# ~/.cortex-slack-bridge/start-hook.sh — see README section "SessionStart hook".

# Start the bridge (hook will auto-start it from here on)
bin/coco-bridge start
bin/coco-bridge send "Hello from the bridge"
```

## Slack commands

```
/help                                   Show this list
/projects                               List registered CoCo projects
/use <name>                             Switch the active project (routes free-text DMs)
/new <project> [initial prompt]         Ask the agent to launch a new CoCo CLI session
/task add [priority] <title> :: <desc>  Create SnowBoard task (priority default: medium)
/task list [status]                     List SnowBoard tasks, optional status filter
/task status <id> <status>              Update SnowBoard task status

<any non-slash text>                    Forward as a "reply" to the active session
```

Valid priorities: `urgent|high|medium|low|none`  
Valid statuses: `backlog|in_progress|need_approval|review|done`

## Architecture

```
You (Slack DM)
   │
   ▼
Slack Socket Mode
   │
   ▼
bridge.py  ──► parses command / dispatches
   │              │
   │              ├── inline (projects/use/help) → replies directly
   │              └── command/reply/confirmation → writes inbox_{session}.json
   │
   ▼
CoCo session (Desktop or CLI)
   │  (fast 5s poll loop inside a */1 cron)
   ▼
Skill invokes SnowBoard tools, responds via coco-bridge send
```

Every outbound Slack message embeds `{session_id, project}` metadata so replies route back to the correct session even with multiple in flight.

## CLI wrapper

```
coco-bridge start|stop|status
coco-bridge send "msg" [--type status|success|warning|error]
coco-bridge confirm "question" --id <id>
coco-bridge inbox
coco-bridge history [N]
coco-bridge setup-keychain | clear-keychain

coco-bridge project list
coco-bridge project register <name> [path]
coco-bridge project use <name>
coco-bridge project remove <name>

coco-bridge task add [priority] "title" ["desc"]
coco-bridge set-poll-interval [seconds]
coco-bridge wake
```

## Configuration

`~/.cortex-slack-bridge/config.json` (optional, Keychain takes priority for tokens):

```json
{
  "poll_interval_seconds": 5,
  "poll_wake_enabled": false,
  "app_token": "xapp-...",
  "bot_token": "xoxb-...",
  "user_id": "U..."
}
```

Environment overrides:

- `SLACK_BRIDGE_APP_TOKEN`, `SLACK_BRIDGE_BOT_TOKEN`, `SLACK_BRIDGE_USER_ID`
- `COCO_BRIDGE_POLL_INTERVAL` (seconds, 2–30)
- `COCO_BRIDGE_WAKE_ENABLED` (`true|false`)

## SessionStart hook

`~/.snowflake/cortex/hooks.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/Users/YOUR_USERNAME/.cortex-slack-bridge/start-hook.sh",
            "timeout": 10,
            "enabled": true
          }
        ]
      }
    ]
  }
}
```

The hook ensures the sidecar is running, registers the CoCo session's CWD as a project, and prompts the agent to offer enabling Slack for this session.

## Credits

Original implementation and hook/skill design by [Dash Desai](https://github.com/iamontheinet) — see his [Medium post](https://medium.com/snowflake/snowflake-cortex-code-cli-meets-slack-8a3ce0a0630c).

This fork adds project-aware routing and SnowBoard integration while keeping the core sidecar architecture unchanged.
