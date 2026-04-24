#!/usr/bin/env bash
# SessionStart hook for the Cortex Code Slack Bridge.
# Outputs a prompt that instructs the agent to ask the user if they want Slack
# enabled for this session. Also auto-registers the current project + session.

set -euo pipefail

BRIDGE="$HOME/Apps/coco-slack-bridge/bin/coco-bridge"
BRIDGE_DIR="$HOME/.cortex-slack-bridge"
PROJECT_NAME="$(basename "$PWD")"

# Resolve session ID.
# CoCo Desktop sets CORTEX_SESSION_ID. CoCo CLI may not — generate a stable
# UUID for this process group and persist it so the skill can reference it.
if [[ -n "${CORTEX_SESSION_ID:-}" ]]; then
    SESSION_ID="$CORTEX_SESSION_ID"
else
    SESSION_FILE="$BRIDGE_DIR/session_${PROJECT_NAME}.id"
    mkdir -p "$BRIDGE_DIR"
    if [[ ! -f "$SESSION_FILE" ]]; then
        uuidgen | tr '[:upper:]' '[:lower:]' > "$SESSION_FILE"
    fi
    SESSION_ID="$(<"$SESSION_FILE")"
    # Export so child processes (notify.py etc.) also see it
    export CORTEX_SESSION_ID="$SESSION_ID"
fi

# Make sure the sidecar is running (idempotent).
"$BRIDGE" status 2>/dev/null | grep -q running || "$BRIDGE" start >/dev/null 2>&1 || true

# Register this project + session in the registry.
"$BRIDGE" project register "$PROJECT_NAME" "$PWD" >/dev/null 2>&1 || true

# Emit the prompt that CoCo will pick up as SessionStart context.
cat <<EOF
[slack-bridge] Session registered under project '$PROJECT_NAME' (session_id=$SESSION_ID).
Ask the user via ask_user_question: "Enable Slack notifications for this session?" with options Yes/No.
If Yes, run the enable flow in the slack-bridge skill (register fast inbox cron, send activation message).
EOF
