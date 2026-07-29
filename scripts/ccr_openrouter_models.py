#!/usr/bin/env python3
"""Apply / verify multi-model OpenRouter pass-through for Claude Code via CCR.

This mutates *host* CCR state under ``~/.claude-code-router`` and
``~/.claude/settings.json``. It does **not** change Hermes runtime code.

Why Terra is special
--------------------
ai-gateway parses ``provider/model`` selectors. The prefix ``openai/`` is treated
as the *OpenAI provider type*, which conflicts when the upstream is OpenRouter.
We therefore register bare ids ``gpt-5.6-terra`` / ``gpt-5.6-terra-pro`` in the
provider catalog and set ``extraBody.byModel`` so the *upstream* request body
uses the real OpenRouter slugs ``openai/gpt-5.6-terra*``.

Usage
-----
  python3 scripts/ccr_openrouter_models.py apply
  python3 scripts/ccr_openrouter_models.py verify   # hits localhost CCR
  python3 scripts/ccr_openrouter_models.py show
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()
CCR = HOME / ".claude-code-router"
DB = CCR / "config.sqlite"
GW = CCR / "gateway.config.json"
SETTINGS = HOME / ".claude" / "settings.json"
BIN = CCR / "bin"
LOCAL_BIN = HOME / ".local" / "bin"
API_KEY_HELPER = BIN / "ccr-claude-code-api-key-default-claude-code"

TERRA_BARE = "gpt-5.6-terra"
TERRA_PRO_BARE = "gpt-5.6-terra-pro"
TERRA_OR = "openai/gpt-5.6-terra"
TERRA_PRO_OR = "openai/gpt-5.6-terra-pro"

MODELS = [
    "z-ai/glm-5.2",
    "x-ai/grok-4.5",
    "x-ai/grok-4.3",
    TERRA_BARE,
    TERRA_PRO_BARE,
    "thinkingmachines/inkling",
    "moonshotai/kimi-k3",
    "moonshotai/kimi-k2.7-code",
]

# Short names Claude Code / humans can pass to --model
ALIASES: dict[str, str] = {
    "glm": "z-ai/glm-5.2",
    "glm-5.2": "z-ai/glm-5.2",
    "grok": "x-ai/grok-4.5",
    "grok-4.5": "x-ai/grok-4.5",
    "terra": TERRA_BARE,
    "gpt-terra": TERRA_BARE,
    "gpt-5.6-terra": TERRA_BARE,
    "openai/gpt-5.6-terra": TERRA_BARE,
    "terra-pro": TERRA_PRO_BARE,
    "gpt-5.6-terra-pro": TERRA_PRO_BARE,
    "openai/gpt-5.6-terra-pro": TERRA_PRO_BARE,
    "inkling": "thinkingmachines/inkling",
    "kimi": "moonshotai/kimi-k3",
    "kimi-3": "moonshotai/kimi-k3",
    "kimi-k3": "moonshotai/kimi-k3",
    "kimi-code": "moonshotai/kimi-k2.7-code",
}

ORCH_PROFILES = {
    "glm": "z-ai/glm-5.2",
    "grok": "x-ai/grok-4.5",
    "terra": TERRA_BARE,
    "inkling": "thinkingmachines/inkling",
    "kimi": "moonshotai/kimi-k3",
}

EXTRA_BODY = {
    "default": {},
    "byModel": {
        TERRA_BARE: {"model": TERRA_OR},
        TERRA_PRO_BARE: {"model": TERRA_PRO_OR},
    },
}

PROVIDER_NAME = "or-openrouter"  # avoid conflicting with openai/ provider type


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat()


def _backup() -> Path:
    dest = HOME / f".claude-code-router-model-expand-bak-{datetime.now(timezone.utc).strftime('%Y%m%d%H%M%S')}"
    dest.mkdir(parents=True, exist_ok=True)
    for p in (DB, GW, SETTINGS):
        if p.exists():
            shutil.copy2(p, dest / p.name)
    return dest


def _alias_rules() -> list[dict]:
    rules = []
    for alias, target in sorted(ALIASES.items()):
        rid = "alias-" + alias.replace("/", "-").replace(".", "-")
        rules.append(
            {
                "id": rid,
                "name": f"Alias {alias} → {target}",
                "type": "condition",
                "enabled": True,
                "condition": {
                    "left": "request.body.model",
                    "operator": "==",
                    "right": alias,
                },
                "rewrites": [
                    {
                        "key": "request.body.model",
                        "operation": "set",
                        "value": target,
                    }
                ],
            }
        )
    return rules


def apply() -> None:
    if not DB.exists():
        raise SystemExit(f"CCR sqlite missing: {DB}. Run `ccr start` once first.")
    bak = _backup()
    print(f"backup → {bak}")

    con = sqlite3.connect(DB)
    row = con.execute("select value_json from app_config where key='default'").fetchone()
    if not row:
        raise SystemExit("app_config.default missing")
    cfg = json.loads(row[0])

    providers = cfg.get("Providers") or []
    if not providers:
        raise SystemExit("No Providers in CCR config")
    for p in providers:
        p["name"] = PROVIDER_NAME
        p["id"] = PROVIDER_NAME
        p["models"] = list(MODELS)
        p["extraBody"] = EXTRA_BODY
        p["extra_body"] = EXTRA_BODY

    rt = cfg.setdefault("Router", {})
    rt.setdefault("builtInRules", {})
    rt["builtInRules"]["claude-code"] = {"enabled": True}
    rt["builtInRules"]["codex"] = {"enabled": True}
    rt["fallback"] = {"mode": "off", "models": [], "retryCount": 1}
    rt["rules"] = _alias_rules()

    # Claude Code profiles (orchestrators)
    prof = cfg.setdefault("profile", {})
    cc = prof.setdefault("claudeCode", {})
    cc["enabled"] = True
    cc["model"] = "z-ai/glm-5.2"
    cc["smallFastModel"] = "z-ai/glm-5.2"
    cc["settingsFile"] = "~/.claude/settings.json"

    profiles = {
        p.get("id"): p
        for p in (prof.get("profiles") or [])
        if isinstance(p, dict) and p.get("id")
    }
    base = profiles.get("default-claude-code") or {
        "agent": "claude-code",
        "enabled": True,
        "id": "default-claude-code",
        "name": "Claude Code",
        "scope": "global",
        "surface": "auto",
        "settingsFile": "~/.claude/settings.json",
    }
    base.update(
        {
            "model": "z-ai/glm-5.2",
            "smallFastModel": "z-ai/glm-5.2",
            "env": {"CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"},
        }
    )
    profiles["default-claude-code"] = base
    for key, model in ORCH_PROFILES.items():
        pid = f"claude-{key}"
        profiles[pid] = {
            "agent": "claude-code",
            "enabled": True,
            "id": pid,
            "name": f"Claude Code ({key})",
            "scope": "global",
            "surface": "auto",
            "settingsFile": "~/.claude/settings.json",
            "model": model,
            "smallFastModel": model,
            "env": {
                "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
                "ANTHROPIC_MODEL": model,
                "CCR_CLAUDE_CODE_MODEL": model,
                "CODEXL_CLAUDE_CODE_MODEL": model,
                "ANTHROPIC_SMALL_FAST_MODEL": model,
            },
        }

    out_profiles: list[dict] = []
    order = ["default-codex", "default-claude-code"] + [
        f"claude-{k}" for k in ORCH_PROFILES
    ]
    seen: set[str] = set()
    for i in order:
        if i in profiles and i not in seen:
            out_profiles.append(profiles[i])
            seen.add(str(i))
    for i, p in profiles.items():
        if i not in seen:
            out_profiles.append(p)
            seen.add(str(i))
    prof["profiles"] = out_profiles
    cfg["profile"] = prof
    cfg["preferredProvider"] = PROVIDER_NAME

    con.execute(
        "update app_config set value_json=?, updated_at=? where key='default'",
        (json.dumps(cfg), _utc()),
    )
    con.commit()
    con.close()

    # Synchronize gateway.config.json (in-memory gateway reads this at boot)
    if GW.exists():
        gw = json.loads(GW.read_text())
    else:
        gw = {"providers": []}
    for p in gw.get("providers") or []:
        name = p.get("name") or ""
        if "::" in name:
            typ = name.split("::", 1)[1]
            p["name"] = f"{PROVIDER_NAME}::{typ}"
        else:
            p["name"] = PROVIDER_NAME
        p["models"] = list(MODELS)
        p["extraBody"] = EXTRA_BODY
    gw["_hermes_openrouter_aliases"] = ALIASES
    gw["_hermes_terra_note"] = (
        "Bare ids gpt-5.6-terra(+pro) + extraBody.byModel rewrite to "
        "openai/gpt-5.6-terra* (openai/ prefix is reserved as provider type)."
    )
    GW.parent.mkdir(parents=True, exist_ok=True)
    GW.write_text(json.dumps(gw, indent=2) + "\n")

    # Default Claude settings → GLM orchestrator, CCR base URL
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    if SETTINGS.exists():
        settings = json.loads(SETTINGS.read_text())
    else:
        settings = {}
    settings["apiKeyHelper"] = str(API_KEY_HELPER)
    env = settings.setdefault("env", {})
    env.update(
        {
            "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1",
            "ANTHROPIC_BASE_URL": "http://127.0.0.1:3456",
            "ANTHROPIC_API_BASE_URL": "http://127.0.0.1:3456",
            "CLAUDE_AGENT_API_BASE_URL": "http://127.0.0.1:3456",
            "ANTHROPIC_MODEL": "z-ai/glm-5.2",
            "CCR_CLAUDE_CODE_MODEL": "z-ai/glm-5.2",
            "CODEXL_CLAUDE_CODE_MODEL": "z-ai/glm-5.2",
            "ANTHROPIC_SMALL_FAST_MODEL": "z-ai/glm-5.2",
        }
    )
    SETTINGS.write_text(json.dumps(settings, indent=2) + "\n")

    _write_wrappers()
    print("apply complete — restart CCR (`ccr stop; ccr start --no-open`) to load.")


def _write_wrapper(path: Path, model: str, profile_id: str, name: str) -> None:
    middleware = BIN / "ccr-codex-cli-middleware.js"
    content = f"""#!/bin/sh
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY='1'
export ANTHROPIC_API_BASE_URL='http://127.0.0.1:3456'
export ANTHROPIC_BASE_URL='http://127.0.0.1:3456'
export CLAUDE_AGENT_API_BASE_URL='http://127.0.0.1:3456'
export CLAUDE_CONFIG_DIR='{HOME}/.claude'
export ANTHROPIC_MODEL='{model}'
export CCR_CLAUDE_CODE_MODEL='{model}'
export CODEXL_CLAUDE_CODE_MODEL='{model}'
export ANTHROPIC_SMALL_FAST_MODEL='{model}'
: "${{CCR_PROFILE_SURFACE:=auto}}"
export CCR_PROFILE_SURFACE
export CCR_BOT_GATEWAY_ENABLED='false'
export CODEXL_BOT_GATEWAY_ENABLED='false'
export CCR_CLAUDE_CODE_WRAPPER=1
export CCR_REAL_CLAUDE_CODE_BIN='claude'
export CODEXL_CLAUDE_CODE_BIN='claude'
if [ -z "${{CCR_REMOTE_SYNC_ENABLED:-}}" ]; then CCR_REMOTE_SYNC_ENABLED=1; fi
if [ -z "${{CCR_REMOTE_SYNC_ENDPOINT:-}}" ]; then CCR_REMOTE_SYNC_ENDPOINT='http://127.0.0.1:3456/__ccr/remote'; fi
if [ -z "${{CCR_REMOTE_SYNC_API_KEY_HELPER:-}}" ]; then CCR_REMOTE_SYNC_API_KEY_HELPER='{API_KEY_HELPER}'; fi
if [ -z "${{CCR_REMOTE_SYNC_PROFILE_ID:-}}" ]; then CCR_REMOTE_SYNC_PROFILE_ID='{profile_id}'; fi
if [ -z "${{CCR_REMOTE_SYNC_PROFILE_NAME:-}}" ]; then CCR_REMOTE_SYNC_PROFILE_NAME='{name}'; fi
export CCR_REMOTE_SYNC_ENABLED CCR_REMOTE_SYNC_ENDPOINT CCR_REMOTE_SYNC_API_KEY_HELPER CCR_REMOTE_SYNC_PROFILE_ID CCR_REMOTE_SYNC_PROFILE_NAME
if command -v node >/dev/null 2>&1; then
  exec node '{middleware}' "$@"
fi
ELECTRON_RUN_AS_NODE=1 exec /usr/bin/node '{middleware}' "$@"
"""
    path.write_text(content)
    path.chmod(0o700)


def _write_wrappers() -> None:
    BIN.mkdir(parents=True, exist_ok=True)
    LOCAL_BIN.mkdir(parents=True, exist_ok=True)
    # default + named
    mapping = {"default": ("z-ai/glm-5.2", "default-claude-code", "Claude Code")}
    for key, model in ORCH_PROFILES.items():
        mapping[key] = (model, f"claude-{key}", f"Claude Code ({key})")
    for key, (model, pid, name) in mapping.items():
        wpath = BIN / f"ccr-claude-code-wrapper-{key}"
        _write_wrapper(wpath, model, pid, name)
        if key == "default":
            shutil.copy2(wpath, BIN / "ccr-claude-code-wrapper-default-claude-code")
        launch = LOCAL_BIN / f"claude-{key if key != 'default' else 'glm'}"
        launch.write_text(
            f"#!/bin/sh\nexec '{wpath}' --model '{model}' \"$@\"\n"
        )
        launch.chmod(0o755)
    print("wrappers:", sorted(p.name for p in LOCAL_BIN.glob("claude-*")))


def show() -> None:
    if not DB.exists():
        print("no CCR db")
        return
    cfg = json.loads(
        sqlite3.connect(DB)
        .execute("select value_json from app_config where key='default'")
        .fetchone()[0]
    )
    for p in cfg.get("Providers") or []:
        print("provider", p.get("name"), "models", p.get("models"))
        print(" extraBody", p.get("extraBody") or p.get("extra_body"))
    print("rules", len((cfg.get("Router") or {}).get("rules") or []))
    print("aliases", ALIASES)
    if GW.exists():
        gw = json.loads(GW.read_text())
        print(
            "gateway providers",
            [(p.get("name"), p.get("models")) for p in gw.get("providers") or []],
        )


def _api_key() -> str:
    if not API_KEY_HELPER.exists():
        raise SystemExit(f"missing api key helper {API_KEY_HELPER}")
    return subprocess.check_output([str(API_KEY_HELPER)], text=True).strip()


def verify(models: list[str] | None = None) -> int:
    key = _api_key()
    models = models or [
        "z-ai/glm-5.2",
        "x-ai/grok-4.5",
        TERRA_BARE,
        TERRA_PRO_BARE,
        "thinkingmachines/inkling",
        "moonshotai/kimi-k3",
        "terra",
        "grok",
        "kimi",
        "inkling",
        "openai/gpt-5.6-terra",  # alias rewrite path
    ]
    rc = 0
    for m in models:
        body = {
            "model": m,
            "max_tokens": 16,
            "messages": [{"role": "user", "content": "Reply with exactly OK"}],
        }
        req = urllib.request.Request(
            "http://127.0.0.1:3456/v1/messages",
            data=json.dumps(body).encode(),
            headers={
                "content-type": "application/json",
                "anthropic-version": "2023-06-01",
                "x-api-key": key,
                "authorization": f"Bearer {key}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                data = json.loads(r.read().decode())
            text = "".join(
                c.get("text", "")
                for c in (data.get("content") or [])
                if c.get("type") == "text"
            )
            print(f"OK  {m:35} {text[:40]!r}")
        except urllib.error.HTTPError as e:
            err = e.read().decode()[:200]
            print(f"FAIL {m:35} {err}")
            rc = 1
        except Exception as e:
            print(f"FAIL {m:35} {e}")
            rc = 1
    return rc


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("cmd", choices=["apply", "verify", "show"])
    args = ap.parse_args(argv)
    if args.cmd == "apply":
        apply()
        return 0
    if args.cmd == "show":
        show()
        return 0
    return verify()


if __name__ == "__main__":
    sys.exit(main())
