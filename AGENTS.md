# Agent Guidelines — coco-slack-bridge (fork)

## What This Repo Is

Bidirectional Slack DM bridge for Cortex Code Desktop + CLI. Fork of [iamontheinet/cortex-code-cli-slack-bridge](https://github.com/iamontheinet/cortex-code-cli-slack-bridge) extended with:

- Project registry + `/projects`, `/use` Slack commands
- SnowBoard task management via `/task add|list|status`
- `/new <project>` to launch new CoCo CLI sessions from Slack
- ~5s polling latency (configurable 2-30s)
- Optional wake-signal for sub-5s response

**Org:** sfc-gh-jusdavis (private)

## Architecture

```
cortex_slack_bridge/
  bridge.py          Socket Mode bot; DM dispatcher (inline | command | reply)
  config.py          Token lookup, poll interval, wake config, session routing
  commands.py        DM command parser (/projects, /use, /task, /new, /help)
  projects.py        Project registry (projects.json)
  notify.py          Outbound messages; embeds {session_id, project} metadata
bin/
  coco-bridge        CLI: start/stop/send/confirm/inbox/history/
                          project {list|register|use|remove}/
                          task add/set-poll-interval/wake/setup-keychain
skill/
  SKILL.md           Cortex Code skill (copy to ~/.snowflake/cortex/skills/slack-bridge/)
demo-start-hook.sh   SessionStart hook (copy to ~/.cortex-slack-bridge/)
```

## Critical Rules

1. **File-based IPC.** Bridge <-> CoCo via inbox JSON files. Do not replace with sockets/Redis.
2. **One inbox per session.** Path: `~/.cortex-slack-bridge/inbox_{session_id}.json`. Free text goes to `get_active_session()`, which consults the project registry first.
3. **Inbox entry types:** `reply` (free text), `command` (structured slash cmd), `confirmation` (button response). Skill handles each.
4. **Command dispatch is split:** inline ops (`/projects`, `/use`, `/help`) answered directly by the bridge; `command`-kind ops (SnowBoard CRUD, `/new`) written to inbox for the skill/agent to execute.
5. **Project metadata.** Every inbox entry and outbound Slack message carries `project = {name, path}`. Skill must match vs. `$(basename "$PWD")` before acting.
6. **Polling is a two-level loop:** `*/1` cron + in-prompt 5-second sleeps (11 iterations / minute). Tuned via `coco-bridge set-poll-interval` or `COCO_BRIDGE_POLL_INTERVAL`.
7. **Wake signal (optional):** when `poll_wake_enabled`, bridge `touch`es `wake_{session}` on inbox write. Skill loops can short-circuit `sleep` via `fswatch -1`.
8. **Token storage priority:** env var > macOS Keychain (`coco-slack-bridge` service) > `config.json`.
9. **History** (`history.jsonl`) is append-only audit. Do not rotate or truncate.

## Auth

- `gh` CLI logged in as `sfc-gh-jusdavis`
- Slack app: Socket Mode only, no public URL
- Tokens in macOS Keychain (service `coco-slack-bridge`, keys `app_token`/`bot_token`/`user_id`)
