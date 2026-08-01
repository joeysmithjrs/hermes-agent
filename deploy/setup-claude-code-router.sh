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
# scrubbing is Claude Code's own settings.json, which Claude Code reads
# for itself at startup. CCR is what manages that file plus the
# Anthropic-Messages <-> OpenRouter protocol translation Claude Code
# needs; see hermes-agent's own subscription-proxy.md, which states that
# translation is explicitly out of scope for Hermes's native `hermes
# proxy` tool.
#
# CONFIG ISOLATION (important — do not "simplify" this back):
# CCR's routing settings live in $HERMES_HOME_DIR/.claude-ccr/settings.json,
# NOT the default ~/.claude/settings.json. The CCR wrappers (claude-glm,
# claude-grok, claude-terra, claude-inkling, claude-kimi) all run with
# CLAUDE_CONFIG_DIR pointed there.
#
# Reason: Claude Code loads `<cwd>/.claude/settings.json` as *project*
# settings in addition to the CLAUDE_CONFIG_DIR user settings. So while
# CCR's config lived at ~/.claude/settings.json, any Claude Code run with
# cwd=/home/hermes silently inherited CCR's `apiKeyHelper`,
# ANTHROPIC_BASE_URL=http://127.0.0.1:3456 and
# ANTHROPIC_SMALL_FAST_MODEL=z-ai/glm-5.2 — including `claude-pro`, which
# is supposed to run on Joe's Claude Pro subscription. `claude-pro`
# already `env -u`'s those variables, but a settings file beats an
# unset env var, so the wrapper could not defend itself. Observed
# symptom: z-ai/glm-5.2 appearing in a claude-pro run's modelUsage with
# real OpenRouter cost, plus the "claude.ai connectors are disabled
# because ANTHROPIC_API_KEY or another auth source is set" banner.
#
# Keeping CCR out of ~/.claude means /home/hermes is no longer a booby
# trap for anything that runs Claude Code from the home directory. The
# final step of this script asserts that and refuses to finish if CCR has
# leaked back in.
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
# Claude Code config dir owned by the CCR/OpenRouter identity. Deliberately
# NOT ~/.claude — see "CONFIG ISOLATION" in the header.
CCR_CLAUDE_CONFIG_DIR="$HERMES_HOME_DIR/.claude-ccr"
CCR_CLAUDE_SETTINGS="$CCR_CLAUDE_CONFIG_DIR/settings.json"
# The default dir CCR must never be allowed to colonise again.
DEFAULT_CLAUDE_SETTINGS="$HERMES_HOME_DIR/.claude/settings.json"
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

echo "=== [1/7] Installing claude-code + claude-code-router as $HERMES_USER ==="
su - "$HERMES_USER" -c "
    set -euo pipefail
    mkdir -p '$NPM_PREFIX'
    npm config set prefix '$NPM_PREFIX'
    export PATH='$HERMES_PATH'
    npm install -g @anthropic-ai/claude-code @musistudio/claude-code-router
"

echo "=== [2/7] Symlinking claude/ccr into /usr/local/bin ==="
ln -sf "$NPM_PREFIX/bin/claude" /usr/local/bin/claude
ln -sf "$NPM_PREFIX/bin/ccr" /usr/local/bin/ccr

echo "=== [3/7] Writing CCR management env file ==="
if [ ! -f "$CCR_ENV_FILE" ]; then
    CCR_WEB_TOKEN="$(openssl rand -hex 16)"
    printf 'CCR_WEB_AUTH_TOKEN=%s\n' "$CCR_WEB_TOKEN" > "$CCR_ENV_FILE"
    chown "$HERMES_USER:$HERMES_USER" "$CCR_ENV_FILE"
    chmod 600 "$CCR_ENV_FILE"
    echo "Generated new CCR_WEB_AUTH_TOKEN (management UI token)."
else
    echo "Reusing existing $CCR_ENV_FILE."
fi

echo "=== [4/7] Writing systemd unit ==="
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

echo "=== [5/7] Stopping service to safely bootstrap config.sqlite ==="
systemctl daemon-reload
systemctl stop ccr-router.service 2>/dev/null || true
# Also stop any ad-hoc `ccr start` detached instance from manual use.
su - "$HERMES_USER" -c "export PATH='$HERMES_PATH'; ccr stop" 2>/dev/null || true

install -d -o "$HERMES_USER" -g "$HERMES_USER" -m 700 "$CCR_CLAUDE_CONFIG_DIR"

su - "$HERMES_USER" -c "OPENROUTER_API_KEY='$OPENROUTER_API_KEY' MODEL='$MODEL' CCR_CONFIG_DIR='$CCR_CONFIG_DIR' CCR_CLAUDE_SETTINGS='$CCR_CLAUDE_SETTINGS' python3" << 'PYEOF'
import json
import os
import secrets
import sqlite3
import datetime

config_dir = os.environ["CCR_CONFIG_DIR"]
os.makedirs(config_dir, exist_ok=True)
db_path = os.path.join(config_dir, "config.sqlite")

# Absolute path, isolated from ~/.claude. CCR writes this file itself, so
# whatever it points at becomes CCR-owned — which is exactly why it must
# not point at the default config dir (see CONFIG ISOLATION in header).
settings_file = os.environ["CCR_CLAUDE_SETTINGS"]

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
            "claudeCode": {"enabled": True, "model": "", "settingsFile": settings_file, "smallFastModel": ""},
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

# --- Provider: OpenRouter entry, merged in place. ---
# Deliberately additive. This used to hard-assign
#   data["Providers"] = [{... "id": "openrouter", "models": [model] ...}]
# which is destructive in two ways on any box that grew past one model:
#   1. it drops every other provider, and
#   2. it truncates the OpenRouter model list to the single CLI argument.
# On this host the live provider is id='or-openrouter' carrying eight models
# (glm-5.2, grok-4.5/4.3, gpt-5.6-terra(+pro), inkling, kimi-k3/k2.7-code) that
# the claude-glm/-grok/-terra/-inkling/-kimi wrappers select with `--model`.
# A re-run under the old logic would have deleted that provider outright and
# left all five wrappers pointing at models CCR no longer advertises.
#
# So: match on api_base_url rather than id (the id is whatever CCR's UI
# happened to mint), refresh the key, and union the model list.
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_CAPABILITIES = [
    {"baseUrl": "https://openrouter.ai/api/v1", "endpoint": "https://openrouter.ai/api/v1/chat/completions", "source": "detected", "type": "openai_chat_completions"},
    {"baseUrl": "https://openrouter.ai/api/v1", "endpoint": "https://openrouter.ai/api/v1/responses", "source": "detected", "type": "openai_responses"},
    {"baseUrl": "https://openrouter.ai/api", "endpoint": "https://openrouter.ai/api/v1/messages", "source": "detected", "type": "anthropic_messages"},
]

providers = [p for p in (data.get("Providers") or []) if isinstance(p, dict)]
openrouter_provider = next(
    (p for p in providers
     if (p.get("api_base_url") or "").rstrip("/") == OPENROUTER_BASE_URL),
    None,
)
if openrouter_provider is None:
    openrouter_provider = {
        "api_base_url": OPENROUTER_BASE_URL,
        "capabilities": OPENROUTER_CAPABILITIES,
        "id": "openrouter",
        "models": [],
        "name": "OpenRouter",
        "type": "openai_chat_completions",
    }
    providers.append(openrouter_provider)
    print("  created OpenRouter provider entry")
else:
    print(f"  reusing existing provider {openrouter_provider.get('id')!r}")

openrouter_provider["api_key"] = openrouter_key
openrouter_provider.setdefault("capabilities", OPENROUTER_CAPABILITIES)
openrouter_provider.setdefault("type", "openai_chat_completions")
openrouter_provider.setdefault("name", "OpenRouter")

known_models = [m for m in (openrouter_provider.get("models") or []) if isinstance(m, str)]
if model not in known_models:
    known_models.append(model)
    print(f"  added model {model!r}")
openrouter_provider["models"] = known_models
print(f"  provider now advertises {len(known_models)} model(s)")

data["Providers"] = providers

# --- Claude Code profile: global scope, forced to `model`. ---
data.setdefault("profile", {}).setdefault("profiles", [])
existing = next((p for p in data["profile"]["profiles"] if p.get("id") == "default-claude-code"), None)
claude_profile = existing or {
    "agent": "claude-code",
    "id": "default-claude-code",
    "name": "Claude Code",
    "scope": "global",
    "surface": "auto",
}
# settingsFile is forced on every run, not just first creation: a profile
# provisioned before config isolation still carries ~/.claude/settings.json,
# and leaving it would let CCR re-colonise the default config dir on the
# next re-run. Same reason it is re-asserted on claudeCode below.
claude_profile.update({
    "enabled": True,
    "env": {"CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"},
    "model": model,
    "settingsFile": settings_file,
    "smallFastModel": model,
})
data["profile"]["profiles"] = [
    p for p in data["profile"]["profiles"] if p.get("id") != "default-claude-code"
] + [claude_profile]
claude_code_cfg = data.setdefault("profile", {}).setdefault("claudeCode", {})
claude_code_cfg["enabled"] = True
claude_code_cfg["model"] = model
claude_code_cfg["smallFastModel"] = model
claude_code_cfg["settingsFile"] = settings_file

# Sweep: any *other* profile still aimed at the default Claude config dir
# would let CCR write there behind our back. Redirect them too.
default_settings_names = {
    "~/.claude/settings.json",
    os.path.expanduser("~/.claude/settings.json"),
}
for prof in data["profile"]["profiles"]:
    if prof.get("settingsFile") in default_settings_names:
        print(f"  redirecting profile {prof.get('id')!r} off the default config dir")
        prof["settingsFile"] = settings_file

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

echo "=== [6/7] Starting service and smoke-testing ==="
systemctl enable --now ccr-router.service
sleep 3
systemctl is-active --quiet ccr-router.service || {
    echo "ERROR: ccr-router.service did not come up. Check: systemctl status ccr-router.service" >&2
    exit 1
}

# CCR treats its managed settingsFile as ephemeral: it writes the file when a
# profile is applied and *restores* it on stop. If a sibling marker named
# settings.json.ccr-original-missing exists, "restore" means DELETE — CCR is
# faithfully putting things back the way it found them. That is fine for CCR's
# own generated wrappers (which re-apply the profile per invocation) but fatal
# for the hand-rolled claude-glm/-grok/-terra/-inkling/-kimi wrappers, which
# invoke `claude` directly and need the file to persist. Symptom when it goes
# missing: `claude-glm` returns "Not logged in · Please run /login".
#
# So: neutralise the marker and make sure the file is present before the smoke
# test, restoring CCR's own most recent backup if the live file is gone.
MARKER="$CCR_CLAUDE_CONFIG_DIR/settings.json.ccr-original-missing"
if [ -f "$MARKER" ]; then
    mv "$MARKER" "$MARKER.disabled-$(date +%Y%m%d%H%M%S)"
    echo "Neutralised $MARKER (it makes CCR delete its own settings file on stop)."
fi
if [ ! -f "$CCR_CLAUDE_SETTINGS" ]; then
    LATEST_BACKUP="$(ls -1t "$CCR_CLAUDE_CONFIG_DIR"/settings.json.ccr-backup-* 2>/dev/null | head -1 || true)"
    if [ -n "$LATEST_BACKUP" ]; then
        cp -a "$LATEST_BACKUP" "$CCR_CLAUDE_SETTINGS"
        chown "$HERMES_USER:$HERMES_USER" "$CCR_CLAUDE_SETTINGS"
        chmod 600 "$CCR_CLAUDE_SETTINGS"
        echo "Restored $CCR_CLAUDE_SETTINGS from $LATEST_BACKUP."
    else
        echo "WARNING: $CCR_CLAUDE_SETTINGS is missing and no CCR backup was found;" >&2
        echo "         the direct claude-* wrappers will report 'Not logged in'." >&2
    fi
fi

# Run from /tmp with the CCR config dir explicitly selected: cwd must not be
# $HERMES_HOME_DIR or Claude Code would pick up whatever project settings
# live there, which is the very coupling this script exists to avoid.
RESULT="$(su - "$HERMES_USER" -c "export PATH='$HERMES_PATH'; cd /tmp && CLAUDE_CONFIG_DIR='$CCR_CLAUDE_CONFIG_DIR' claude -p 'Reply with only the word: ok' --max-turns 1 --output-format json" 2>&1)"
echo "$RESULT"
echo "$RESULT" | grep -q '"is_error":false' && echo "=== Claude Code -> OpenRouter ($MODEL) verified working. ===" || {
    echo "ERROR: smoke test did not report success — check output above and 'systemctl status ccr-router.service'." >&2
    exit 1
}

echo "=== [7/7] Asserting CCR has not leaked into the default Claude config ==="
# CCR writes its own settingsFile, so a mis-provisioned profile silently
# turns ~/.claude/settings.json into a CCR file — which every Claude Code
# run with cwd=$HERMES_HOME_DIR then inherits as project settings,
# including claude-pro. Self-heal it here rather than leaving a live leak.
if [ -f "$DEFAULT_CLAUDE_SETTINGS" ] \
   && grep -qE '"(apiKeyHelper|ANTHROPIC_BASE_URL|ANTHROPIC_API_BASE_URL|CLAUDE_AGENT_API_BASE_URL|ANTHROPIC_MODEL|ANTHROPIC_SMALL_FAST_MODEL|CCR_CLAUDE_CODE_MODEL)"' "$DEFAULT_CLAUDE_SETTINGS"; then
    BACKUP="$DEFAULT_CLAUDE_SETTINGS.ccr-leak-$(date +%Y%m%d%H%M%S)"
    cp -a "$DEFAULT_CLAUDE_SETTINGS" "$BACKUP"
    printf '{}\n' > "$DEFAULT_CLAUDE_SETTINGS"
    chown "$HERMES_USER:$HERMES_USER" "$DEFAULT_CLAUDE_SETTINGS"
    echo "WARNING: CCR routing keys were found in $DEFAULT_CLAUDE_SETTINGS." >&2
    echo "         Neutralised to '{}'; previous contents saved to $BACKUP." >&2
    echo "         CCR config belongs in $CCR_CLAUDE_SETTINGS — check CCR's" >&2
    echo "         profile settingsFile if this recurs." >&2
else
    echo "OK: $DEFAULT_CLAUDE_SETTINGS is free of CCR routing keys."
fi

# The wrappers are provisioned outside this script; warn (don't fail) if any
# CCR wrapper still selects the default config dir.
STRAY_WRAPPERS=""
for w in claude-glm claude-grok claude-terra claude-inkling claude-kimi; do
    wp="$HERMES_HOME_DIR/.local/bin/$w"
    [ -f "$wp" ] || continue
    if grep -qE "CLAUDE_CONFIG_DIR=$HERMES_HOME_DIR/\.claude(\s|$|\\\\)" "$wp"; then
        STRAY_WRAPPERS="$STRAY_WRAPPERS $w"
    fi
done
if [ -n "$STRAY_WRAPPERS" ]; then
    echo "WARNING: these CCR wrappers still point at the default config dir:$STRAY_WRAPPERS" >&2
    echo "         Repoint them to CLAUDE_CONFIG_DIR=$CCR_CLAUDE_CONFIG_DIR." >&2
else
    echo "OK: CCR wrappers use $CCR_CLAUDE_CONFIG_DIR."
fi
