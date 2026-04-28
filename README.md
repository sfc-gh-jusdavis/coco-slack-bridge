# CoCo Slack Bridge

Bidirectional Slack DM bridge for [Cortex Code](https://docs.snowflake.com/en/user-guide/cortex-code/cortex-code-cli) (Desktop + CLI). Send messages from Slack, get immediate responses — no polling, no terminal required.

> Fork of [iamontheinet/cortex-code-cli-slack-bridge](https://github.com/iamontheinet/cortex-code-cli-slack-bridge) with headless dispatch, project management, and session management from Slack.

## How It Works

```
You (Slack DM)
   │
   ▼
Socket Mode (always-on daemon)
   │
   ▼
bridge.py ──► parses !command or free text
   │
   ├── !commands → answered directly (status, projects, help, etc.)
   │
   └── free text → cortex -p --output-format stream-json
          │
          ▼
       Response posted back to Slack thread
```

**Headless mode** (default): Messages run `cortex -p` as a subprocess with `--output-format stream-json`. Responses come back immediately to your Slack thread. Multi-turn conversations are preserved via `--resume <session_id>`.

**Terminal mode** (legacy): Messages are written to inbox JSON files for cron-based polling by an active CoCo session.

Each Slack thread maps to its own cortex session. Start new sessions with `!new`, continue conversations by replying in a thread.

## Prerequisites

- **macOS** (uses `launchd` for daemon management, Keychain for token storage)
- **Python 3.10+**
- **Cortex Code CLI** installed and on your `PATH` (`cortex --version` should work)
- **Slack workspace** where you can create apps

## Setup

### 1. Create a Slack App

1. Go to [api.slack.com/apps](https://api.slack.com/apps) → **Create New App** → **From scratch**
2. Name it `<YourName> Bridge` (e.g., `Justin Bridge`) — each user creates their own app, so unique names avoid confusion in the workspace

**Enable Socket Mode:**
3. Go to **Socket Mode** (left sidebar) → toggle **Enable Socket Mode** on
4. Create an App-Level Token with scope `connections:write` — copy the `xapp-...` token

**Add Bot Scopes:**
5. Go to **OAuth & Permissions** → **Bot Token Scopes** → add these scopes:
   - `chat:write` — send messages
   - `im:write` — send DMs
   - `im:history` — read DM history
   - `im:read` — access DM channels
   - `reactions:write` — add emoji reactions (acknowledgment indicators)

**Enable Events:**
6. Go to **Event Subscriptions** → toggle **Enable Events** on
7. Under **Subscribe to bot events** → add `message.im`

**Install:**
8. Go to **Install App** → **Install to Workspace** → authorize
9. Copy the **Bot User OAuth Token** (`xoxb-...`) from the OAuth & Permissions page

**Get your User ID:**
10. In Slack, click your profile → **⋮ More** → **Copy member ID** — this is your `U...` ID

### 2. Install the Bridge

```bash
git clone https://github.com/sfc-gh-jusdavis/coco-slack-bridge.git ~/Apps/coco-slack-bridge
cd ~/Apps/coco-slack-bridge
python3 -m venv .venv
.venv/bin/pip install -e .
```

### 3. Store Tokens

```bash
bin/coco-bridge setup-keychain
```

This will prompt for your three tokens:
- **App token** (`xapp-...`) — from Socket Mode setup
- **Bot token** (`xoxb-...`) — from OAuth & Permissions
- **User ID** (`U...`) — your Slack member ID

Alternatively, use environment variables or `~/.cortex-slack-bridge/config.json`:

```json
{
  "app_token": "xapp-...",
  "bot_token": "xoxb-...",
  "user_id": "U..."
}
```

### 4. Set Up the Daemon

Copy and customize the launchd plist:

```bash
# Copy template
cp com.coco.slack-bridge.plist.template ~/Library/LaunchAgents/com.coco.slack-bridge.plist

# Replace __HOME__ with your home directory
sed -i '' "s|__HOME__|$HOME|g" ~/Library/LaunchAgents/com.coco.slack-bridge.plist

# Load the daemon (starts immediately, auto-restarts on crash, runs on login)
launchctl load ~/Library/LaunchAgents/com.coco.slack-bridge.plist
```

**Verify it's running:**
```bash
bin/coco-bridge status
# or check logs:
tail -f ~/Library/Logs/coco-slack-bridge.stderr.log
```

### 5. Optional: Install the CoCo Skill

The skill enables CoCo sessions to auto-register projects and prompt for Slack integration:

```bash
mkdir -p ~/.snowflake/cortex/skills/slack-bridge
cp skill/SKILL.md ~/.snowflake/cortex/skills/slack-bridge/SKILL.md
```

## Slack Commands

All commands use the `!` prefix.

| Command | Description |
|---------|-------------|
| `!help` | Show all commands |
| `!status` | Bridge mode, active project, session count |
| `!projects` | List registered projects and sessions |
| `!sessions` | Detailed headless session list |
| `!threads` | Thread-to-session mappings |
| `!new <project> [prompt]` | Start a new headless session in a project |
| `!open [project]` | Open interactive Terminal.app (in a thread: resumes that session) |
| `!use <name>` | Switch the active project for routing |
| `!kill <thread_ts>` | Remove a thread-session mapping |
| `!mode [headless\|terminal]` | Show or switch bridge mode |
| `!task add [priority] <title> :: <desc>` | Create a SnowBoard task |
| `!task list [status]` | List tasks |
| `!task status <id> <status>` | Update a task |

Free text (no `!` prefix) is sent to the active cortex session. In a thread, it continues that thread's session.

## Configuration

`~/.cortex-slack-bridge/config.json` (all fields optional):

```json
{
  "bridge_mode": "headless",
  "headless_timeout": 120,
  "headless_max_turns": 10,
  "poll_interval_seconds": 5,
  "poll_wake_enabled": false
}
```

**Environment variable overrides:**

| Variable | Description | Default |
|----------|-------------|---------|
| `COCO_BRIDGE_MODE` | `headless` or `terminal` | `headless` |
| `COCO_HEADLESS_TIMEOUT` | Seconds per cortex invocation | `120` |
| `COCO_HEADLESS_MAX_TURNS` | Max agentic turns per invocation | `10` |
| `COCO_BRIDGE_POLL_INTERVAL` | Poll interval in terminal mode (2-30s) | `5` |
| `COCO_BRIDGE_WAKE_ENABLED` | Enable wake-signal for terminal mode | `false` |
| `SLACK_BRIDGE_APP_TOKEN` | Override Keychain app token | — |
| `SLACK_BRIDGE_BOT_TOKEN` | Override Keychain bot token | — |
| `SLACK_BRIDGE_USER_ID` | Override Keychain user ID | — |

## CLI

```bash
coco-bridge start              # Start the bridge (foreground or daemon)
coco-bridge stop               # Stop the bridge
coco-bridge status             # Check if running
coco-bridge send "message"     # Send a notification to Slack
coco-bridge confirm "question" # Send Approve/Deny buttons
coco-bridge logs               # Tail bridge logs
coco-bridge inbox              # Show current inbox
coco-bridge history [N]        # Show last N audit entries
coco-bridge setup-keychain     # Store tokens in macOS Keychain
coco-bridge clear-keychain     # Remove tokens from Keychain
coco-bridge project list|register|use|remove
coco-bridge set-poll-interval [seconds]
```

## Managing the Daemon

```bash
# Check if running
launchctl list | grep coco

# Stop
launchctl unload ~/Library/LaunchAgents/com.coco.slack-bridge.plist

# Start
launchctl load ~/Library/LaunchAgents/com.coco.slack-bridge.plist

# View logs
tail -f ~/Library/Logs/coco-slack-bridge.stderr.log
```

The daemon auto-starts on login and auto-restarts on crash.

## Troubleshooting

**Bridge not responding to Slack messages:**
- Check logs: `tail -20 ~/Library/Logs/coco-slack-bridge.stderr.log`
- Verify daemon is running: `bin/coco-bridge status`
- Ensure tokens are set: `security find-generic-password -s coco-slack-bridge -a bot_token -w`

**"missing_scope" errors in logs:**
- Go to your Slack app → OAuth & Permissions → add the missing scope
- Reinstall the app to your workspace

**cortex not found:**
- Ensure `cortex` is on the daemon's PATH
- The plist sets PATH to `.venv/bin:/usr/local/bin:/usr/bin:/bin`
- If cortex is elsewhere (e.g., `~/.local/bin`), add it to the plist's PATH

**Terminal popup when using `!open`:**
- macOS may prompt to allow Python to control Terminal.app
- Allow it once — macOS remembers the permission

**Responses are slow or timing out:**
- Increase timeout: set `headless_timeout` in config.json (default 120s)
- Increase max turns: set `headless_max_turns` (default 10)

## Architecture

```
cortex_slack_bridge/
  bridge.py          Socket Mode bot; DM dispatcher (headless | terminal)
  config.py          Token lookup, mode config, session routing
  commands.py        DM command parser (!new, !open, !mode, !sessions, etc.)
  sessions.py        Headless session manager (cortex -p subprocess runner)
  projects.py        Project registry (projects.json)
  notify.py          Outbound Slack messages with session/project metadata
bin/
  coco-bridge        CLI wrapper (start/stop/send/confirm/inbox/project/etc.)
skill/
  SKILL.md           Cortex Code skill (optional, for auto-registration)
```

## Credits

Original implementation by [Dash Desai](https://github.com/iamontheinet) — see his [Medium post](https://medium.com/snowflake/snowflake-cortex-code-cli-meets-slack-8a3ce0a0630c).

This fork adds headless dispatch, project-aware routing, session management, and SnowBoard integration.
