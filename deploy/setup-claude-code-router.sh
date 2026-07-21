#!/bin/bash
# Provisions claude-code-router (CCR) so the Claude Code CLI — invoked by
# Hermes as a subprocess via the `claude-code` skill — routes through
# OpenRouter instead of Anthropic's own API.
#
# Why this exists instead of a native Hermes provider setting: Claude Code
# is Anthropic's own binary, not Hermes code. Hermes's provider config
# (model.provider, delegation.*) only governs calls Hermes's own process
# makes for itself; it has no hook into how a subprocess it merely shells
# out to picks an endpoint. Hermes also hard-blocks ANTHROPIC_API_KEY /
# ANTHROPIC_BASE_URL / OPENROUTER_API_KEY from ever reaching a
# terminal/execute_code child (see tools/environments/local.py,
# GHSA-rhgp-j443-p4rf) — that's deliberate and not configurable, so env
# vars can't carry this either. The one surface that's unaffected by that
# scrubbing is Claude Code's own ~/.claude/settings.json, which Claude
# Code reads for itself at startup. CCR is what manages that file plus
# the Anthropic-Messages <-> OpenRouter protocol translation Claude Code
# needs; see hermes-agent's own subscription-proxy.md, which states that
# translation is explicitly out of scope for Hermes's native `hermes
# proxy` tool.
#
# Safe to re-run. Must run as root (installs a systemd unit, symlinks
# into /usr/local/bin). Targets the `hermes` system user created for the
# gateway service.
#
# Usage:
#   sudo ./setup-claude-code-router.sh [model]
#   sudo ./setup-claude-code-router.sh z-ai/glm-5.2
#
# `model` must be a valid OpenRouter model slug. Defaults to z-ai/glm-5.2.

set -euo pipefail

MODEL="${1:-z-ai/glm-5.2}"
HERMES_USER="hermes"
HERMES_HOME_DIR="/home/hermes"
HERMES_ENV_FILE="$HERMES_HOME_DIR/.hermes/.env"
NPM_PREFIX="$HERMES_HOME_DIR/.npm-global"
CCR_CONFIG_DIR="$HERMES_HOME_DIR/.claude-code-router"
CCR_ENV_FILE="$HERMES_HOME_DIR/.claude-code-router.env"
CCR_SERVICE_FILE="/etc/systemd/system/ccr-router.service"
# Built explicitly (rather than "$NPM_PREFIX/bin:\$PATH") because that
# self-referencing form breaks under the su -c string nesting below --
# the escaped \$PATH gets flattened to a literal, non-expanding "$PATH"
# by the time it reaches hermes's shell, clobbering PATH entirely.
HERMES_PATH="$NPM_PREFIX/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin:/usr/games:/usr/local/games:/snap/bin"

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: run as root (needs to write /etc/systemd/system and /usr/local/bin)." >&2
    exit 1
fi

if ! id "$HERMES_USER" >/dev/null 2>&1; then
    echo "ERROR: system user '$HERMES_USER' does not exist. Create it before running this." >&2
    exit 1
fi

if [ ! -f "$HERMES_ENV_FILE" ]; then
    echo "ERROR: $HERMES_ENV_FILE not found — expected Hermes's own .env with OPENROUTER_API_KEY." >&2
    exit 1
fi

OPENROUTER_API_KEY="$(grep -E '^OPENROUTER_API_KEY=' "$HERMES_ENV_FILE" | head -1 | cut -d= -f2-)"
if [ -z "$OPENROUTER_API_KEY" ]; then
    echo "ERROR: OPENROUTER_API_KEY not set in $HERMES_ENV_FILE." >&2
    exit 1
fi

echo "=== [1/6] Installing claude-code + claude-code-router as $HERMES_USER ==="
su - "$HERMES_USER" -c "
    set -euo pipefail
    mkdir -p '$NPM_PREFIX'
    npm config set prefix '$NPM_PREFIX'
    export PATH='$HERMES_PATH'
    npm install -g @anthropic-ai/claude-code @musistudio/claude-code-router
"

echo "=== [2/6] Symlinking claude/ccr into /usr/local/bin ==="
ln -sf "$NPM_PREFIX/bin/claude" /usr/local/bin/claude
ln -sf "$NPM_PREFIX/bin/ccr" /usr/local/bin/ccr

echo "=== [3/6] Writing CCR management env file ==="
if [ ! -f "$CCR_ENV_FILE" ]; then
    CCR_WEB_TOKEN="$(openssl rand -hex 16)"
    printf 'CCR_WEB_AUTH_TOKEN=%s\n' "$CCR_WEB_TOKEN" > "$CCR_ENV_FILE"
    chown "$HERMES_USER:$HERMES_USER" "$CCR_ENV_FILE"
    chmod 600 "$CCR_ENV_FILE"
    echo "Generated new CCR_WEB_AUTH_TOKEN (management UI token)."
else
    echo "Reusing existing $CCR_ENV_FILE."
fi

echo "=== [4/6] Writing systemd unit ==="
cat > "$CCR_SERVICE_FILE" << EOF
[Unit]
Description=Claude Code Router (Anthropic-compatible proxy to OpenRouter)
After=network.target

[Service]
Type=simple
User=$HERMES_USER
Group=$HERMES_USER
WorkingDirectory=$HERMES_HOME_DIR
EnvironmentFile=$CCR_ENV_FILE
Environment=PATH=$NPM_PREFIX/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=$NPM_PREFIX/bin/ccr serve --host 127.0.0.1 --port 3458 --no-open --gateway
Restart=always
RestartSec=5
KillSignal=SIGTERM
TimeoutStopSec=30
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$CCR_SERVICE_FILE"

echo "=== [5/6] Stopping service to safely bootstrap config.sqlite ==="
systemctl daemon-reload
systemctl stop ccr-router.service 2>/dev/null || true
# Also stop any ad-hoc `ccr start` detached instance from manual use.
su - "$HERMES_USER" -c "export PATH='$HERMES_PATH'; ccr stop" 2>/dev/null || true

su - "$HERMES_USER" -c "OPENROUTER_API_KEY='$OPENROUTER_API_KEY' MODEL='$MODEL' CCR_CONFIG_DIR='$CCR_CONFIG_DIR' python3" << 'PYEOF'
import json
import os
import secrets
import sqlite3
import datetime

config_dir = os.environ["CCR_CONFIG_DIR"]
os.makedirs(config_dir, exist_ok=True)
db_path = os.path.join(config_dir, "config.sqlite")

openrouter_key = os.environ["OPENROUTER_API_KEY"]
model = os.environ["MODEL"]
now = datetime.datetime.now(datetime.UTC).isoformat()

# NOTE: this reaches into CCR's internal SQLite schema directly because
# CCR (as of the version installed 2026-07) ships no CLI/API for
# provider/model/profile configuration outside its browser UI, and that
# UI's tab navigation does not function when run headlessly (outside its
# Electron shell) — see the setup writeup for how this was discovered.
# This is inherently coupled to CCR's current internal schema and may
# break on a CCR upgrade; re-derive the schema (dump app_config's
# value_json) if this script starts failing after `npm update -g
# @musistudio/claude-code-router`.

con = sqlite3.connect(db_path)
cur = con.cursor()
cur.execute(
    "CREATE TABLE IF NOT EXISTS app_config ("
    "key TEXT PRIMARY KEY, value_json TEXT NOT NULL, updated_at TEXT NOT NULL)"
)
cur.execute("SELECT value_json FROM app_config WHERE key = 'default'")
row = cur.fetchone()

if row is None:
    # First run on this box: minimal skeleton: CCR fills in the rest of
    # its defaults (theme, widgets, etc.) the first time it starts against
    # a config missing those keys.
    data = {
        "APIKEY": "",
        "APIKEYS": [],
        "HOST": "127.0.0.1",
        "PORT": 3456,
        "Providers": [],
        "Router": {
            "builtInRules": {"claude-code": {"enabled": True}, "codex": {"enabled": True}},
            "fallback": {"mode": "off", "models": [], "retryCount": 1},
            "rules": [],
        },
        "gateway": {
            "coreHost": "127.0.0.1",
            "corePort": 3457,
            "enabled": True,
            "generatedConfigFile": os.path.join(config_dir, "gateway.config.json"),
            "host": "127.0.0.1",
            "port": 3456,
        },
        "profile": {
            "claudeCode": {"enabled": True, "model": "", "settingsFile": "~/.claude/settings.json", "smallFastModel": ""},
            "codex": {
                "cliMiddleware": True, "codexCliPath": "", "codexHome": "",
                "configFormat": "separate_profile_files", "configFile": "~/.codex/config.toml",
                "enabled": True, "model": "", "providerId": "claude-code-router",
                "providerName": "Claude Code Router", "showAllSessions": False,
            },
            "enabled": True,
            "profiles": [],
        },
    }
else:
    data = json.loads(row[0])

# --- Provider: single OpenRouter entry, exactly the given model. ---
data["Providers"] = [{
    "api_base_url": "https://openrouter.ai/api/v1",
    "api_key": openrouter_key,
    "capabilities": [
        {"baseUrl": "https://openrouter.ai/api/v1", "endpoint": "https://openrouter.ai/api/v1/chat/completions", "source": "detected", "type": "openai_chat_completions"},
        {"baseUrl": "https://openrouter.ai/api/v1", "endpoint": "https://openrouter.ai/api/v1/responses", "source": "detected", "type": "openai_responses"},
        {"baseUrl": "https://openrouter.ai/api", "endpoint": "https://openrouter.ai/api/v1/messages", "source": "detected", "type": "anthropic_messages"},
    ],
    "id": "openrouter",
    "models": [model],
    "name": "OpenRouter",
    "type": "openai_chat_completions",
}]

# --- Claude Code profile: global scope, forced to `model`. ---
data.setdefault("profile", {}).setdefault("profiles", [])
existing = next((p for p in data["profile"]["profiles"] if p.get("id") == "default-claude-code"), None)
claude_profile = existing or {
    "agent": "claude-code",
    "id": "default-claude-code",
    "name": "Claude Code",
    "scope": "global",
    "settingsFile": "~/.claude/settings.json",
    "surface": "auto",
}
claude_profile.update({
    "enabled": True,
    "env": {"CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"},
    "model": model,
    "smallFastModel": model,
})
data["profile"]["profiles"] = [
    p for p in data["profile"]["profiles"] if p.get("id") != "default-claude-code"
] + [claude_profile]
data["profile"]["claudeCode"]["model"] = model
data["profile"]["claudeCode"]["smallFastModel"] = model

# --- Client API key: keep an existing one if present, else mint one. ---
def is_placeholder(entry):
    return entry.get("key") == "sk-123" and entry.get("createdAt") == datetime.datetime(1970, 1, 1).isoformat()

existing_keys = [k for k in data.get("APIKEYS", []) if isinstance(k, dict) and k.get("key") and not is_placeholder(k)]
if existing_keys:
    keys = existing_keys
else:
    keys = [{
        "id": "default-claude-code-key",
        "key": f"ccr-profile-{secrets.token_urlsafe(24)}",
        "label": "Claude Code (hermes, global)",
        "createdAt": now,
    }]
data["APIKEYS"] = keys
data["APIKEY"] = keys[0]["key"]

cur.execute(
    "INSERT INTO app_config (key, value_json, updated_at) VALUES ('default', ?, ?) "
    "ON CONFLICT(key) DO UPDATE SET value_json = excluded.value_json, updated_at = excluded.updated_at",
    (json.dumps(data), now),
)
con.commit()
con.close()
print(f"Config bootstrapped: model={model!r}, client key id={keys[0]['id']!r}")
PYEOF

chown -R "$HERMES_USER:$HERMES_USER" "$CCR_CONFIG_DIR"

echo "=== [6/6] Starting service and smoke-testing ==="
systemctl enable --now ccr-router.service
sleep 3
systemctl is-active --quiet ccr-router.service || {
    echo "ERROR: ccr-router.service did not come up. Check: systemctl status ccr-router.service" >&2
    exit 1
}

RESULT="$(su - "$HERMES_USER" -c "export PATH='$HERMES_PATH'; claude -p 'Reply with only the word: ok' --max-turns 1 --output-format json" 2>&1)"
echo "$RESULT"
echo "$RESULT" | grep -q '"is_error":false' && echo "=== Claude Code -> OpenRouter ($MODEL) verified working. ===" || {
    echo "ERROR: smoke test did not report success — check output above and 'systemctl status ccr-router.service'." >&2
    exit 1
}
