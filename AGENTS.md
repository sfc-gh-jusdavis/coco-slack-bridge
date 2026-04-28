# Agent Guidelines — coco-slack-bridge

## What This Repo Is

Bidirectional Slack DM bridge for Cortex Code Desktop + CLI. Fork of [iamontheinet/cortex-code-cli-slack-bridge](https://github.com/iamontheinet/cortex-code-cli-slack-bridge) extended with:

- **Headless mode** — runs `cortex -p` with `--output-format stream-json` for zero-polling immediate responses
- Project registry + `!projects`, `!use` Slack commands
- Session management: `!new`, `!open`, `!sessions`, `!kill`
- SnowBoard task management via `!task add|list|status`
- `!mode` to switch between headless and terminal modes
- macOS `launchd` daemon for always-on operation

## Architecture

```
cortex_slack_bridge/
  bridge.py          Socket Mode bot; DM dispatcher (headless | terminal)
                     - Headless: runs cortex -p in ThreadPoolExecutor, replies in-thread
                     - Terminal: writes to inbox JSON for cron-based polling
  config.py          Token lookup (env > Keychain > config.json), mode config,
                     poll interval, session routing, thread-session registry
  commands.py        DM command parser (!new, !open, !mode, !sessions, !kill, etc.)
  sessions.py        Headless session manager — runs cortex -p subprocesses,
                     parses stream-json output, manages thread→session registry
  projects.py        Project registry (projects.json)
  notify.py          Outbound messages; embeds {session_id, project} metadata
bin/
  coco-bridge        CLI: start/stop/send/confirm/inbox/history/
                         project {list|register|use|remove}/
                         task add/set-poll-interval/wake/setup-keychain
skill/
  SKILL.md           Cortex Code skill (copy to ~/.snowflake/cortex/skills/slack-bridge/)
```

## Critical Rules

1. **Headless is the default mode.** Free-text DMs dispatch to `cortex -p` via `_dispatch_headless()`. Each Slack thread maps to a cortex session via `sessions.py`.
2. **Thread-session mapping.** `headless_sessions.json` maps `thread_ts → {session_id, project_path, project_name}`. `sessions.register_session()` creates mappings, `sessions.get_session_for_thread()` looks them up.
3. **ThreadPoolExecutor (3 workers).** Cortex subprocesses run in a thread pool to avoid blocking the Slack event loop. Never run cortex on the main thread.
4. **Stream-JSON protocol.** `cortex -p` with `--output-format stream-json` emits `{type:"system", subtype:"init", session_id:...}`, `{type:"assistant", message:{content:[{type:"text", text:...}]}}`, `{type:"result"}`. Parse all three.
5. **Multi-turn via --resume.** When a thread has an existing session, pass `--resume <session_id>` to continue the conversation.
6. **Terminal mode is legacy.** `_dispatch_terminal()` writes to inbox JSON files for cron-based polling. Still works but not the default.
7. **Command dispatch is split:** inline ops (`!help`, `!status`, `!projects`, `!open`, `!sessions`, `!kill`, `!mode`) answered directly by the bridge; `command`-kind ops (SnowBoard CRUD) written to inbox.
8. **Token storage priority:** env var > macOS Keychain (`coco-slack-bridge` service) > `config.json`.
9. **History** (`history.jsonl`) is append-only audit. Do not rotate or truncate.
10. **Each user runs their own instance.** The bridge filters DMs by `user_id` and runs local `cortex` processes. This is a personal tool, not a shared service.
