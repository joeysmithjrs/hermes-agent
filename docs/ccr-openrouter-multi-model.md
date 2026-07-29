# Claude Code ↔ OpenRouter multi-model (via CCR)

Host-level pass-through so **Claude Code** can orchestrate on non-Anthropic
models through **Claude Code Router** + **OpenRouter**, matching the existing
GLM setup.

> This is **operator config**, not a Hermes runtime feature. Hermes merge/deploy
> simply carries the helper script + docs. Applying it mutates
> `~/.claude-code-router` and `~/.claude/settings.json` on the machine.

## Models

| Role | OpenRouter / CCR id | Notes |
|------|---------------------|--------|
| GLM (default) | `z-ai/glm-5.2` | Same path as before |
| Grok | `x-ai/grok-4.5` (+ `x-ai/grok-4.3` allowed) | |
| GPT Terra | **`gpt-5.6-terra`** (bare) | Upstream slug is `openai/gpt-5.6-terra` via `extraBody` |
| Terra Pro | **`gpt-5.6-terra-pro`** | Upstream `openai/gpt-5.6-terra-pro` |
| Inkling | `thinkingmachines/inkling` | |
| Kimi 3 | `moonshotai/kimi-k3` | Also `moonshotai/kimi-k2.7-code` |
| Aliases | `glm`, `grok`, `terra`, `inkling`, `kimi`, … | Rewritten by CCR Router rules |

### Why Terra is bare

ai-gateway parses `provider/model`. The prefix `openai/` is reserved as the
**OpenAI provider type**, so `openai/gpt-5.6-terra` conflicts when the only
upstream is OpenRouter. Fix:

1. Catalog bare ids `gpt-5.6-terra` / `gpt-5.6-terra-pro`
2. `extraBody.byModel[id] = { "model": "openai/gpt-5.6-terra…" }` so the
   **upstream** OpenRouter body carries the real slug
3. Alias `openai/gpt-5.6-terra` → bare id so old selectors still work

Provider entry is renamed **`or-openrouter`** (not `OpenRouter` /
`openrouter::…` alone) to reduce selector ambiguity.

## Apply on a host

```bash
# CCR must already work for GLM (provider + OR API key in CCR UI/sqlite)
python3 scripts/ccr_openrouter_models.py apply
ccr stop || true
ccr start --no-open
python3 scripts/ccr_openrouter_models.py verify
```

Launchers installed under `~/.local/bin` (on apply):

```bash
claude-glm …
claude-grok …
claude-terra …
claude-inkling …
claude-kimi …
# or
claude --model x-ai/grok-4.5 …
claude --model gpt-5.6-terra …
```

Default `~/.claude/settings.json` still pins **GLM** as the primary
orchestrator (`ANTHROPIC_MODEL=z-ai/glm-5.2`).

## Orchestrator + subagents with different models

### Orchestrator(s)

- **One process → one primary model** via `--model` / launcher / CCR profile
  (`claude-terra`, etc.).
- CCR profiles also exist as `claude-glm`, `claude-grok`, … for `ccr <profile>`.

### Subagents (varying models)

Claude Code’s **Task** tool / agent definitions can set a per-agent `model`.
With gateway model discovery on, any id in the CCR allowlist is fair game.

Examples of intent (exact agent UX depends on your Claude Code version):

| Subagent job | Suggested model |
|--------------|-----------------|
| Fast code edit | `moonshotai/kimi-k2.7-code` or `gpt-5.6-terra` |
| Broad reasoning | `x-ai/grok-4.5` or `thinkingmachines/inkling` |
| Cheap scout | `z-ai/glm-5.2` |
| Orchestrator stays on | your chosen primary |

**Pattern that works today:** keep the parent on GLM (or Grok/Terra), and pass
`--model <id>` when spawning specialized Claude Code runs / Task agents so the
child’s Anthropic-compatible requests hit CCR with a different body.model.

There is **no magic auto-router** that splits every Task by skill unless you
define agents with explicit models. Don’t rely on built-in Anthropic model
escalation — provider allowlist is OpenRouter-only after apply.

### Multi-model in one session

```text
Parent (claude --model z-ai/glm-5.2)
  ├─ Task agent A  model=x-ai/grok-4.5
  ├─ Task agent B  model=gpt-5.6-terra
  └─ Task agent C  model=moonshotai/kimi-k3
```

All Requests: Claude Code → `http://127.0.0.1:3456` → OpenRouter.

## Verify matrix (expected)

`scripts/ccr_openrouter_models.py verify` should print OK for:

- full slugs: glm, grok, inkling, kimi, bare terra(+pro)
- aliases: `terra`, `grok`, `kimi`, `inkling`, `openai/gpt-5.6-terra`

Claude Code smoke (after settings/apiKeyHelper intact):

```bash
claude -p 'Reply ORCH_OK' --model z-ai/glm-5.2 --max-turns 1 --output-format json
claude -p 'Reply ORCH_OK' --model x-ai/grok-4.5 --max-turns 1 --output-format json
claude -p 'Reply ORCH_OK' --model gpt-5.6-terra --max-turns 1 --output-format json
claude -p 'Reply ORCH_OK' --model thinkingmachines/inkling --max-turns 1 --output-format json
claude -p 'Reply ORCH_OK' --model moonshotai/kimi-k3 --max-turns 1 --output-format json
```

## Restart note

Gateway reads `gateway.config.json` **at process start**. After `apply`, always:

```bash
ccr stop; ccr start --no-open
```

Do not leave a 15h-old `ccr serve` holding :3456 — it will ignore sqlite/model
updates.

## Security / ops

- OpenRouter key stays inside CCR provider config (not this repo).
- Script backs up sqlite + gateway + settings before mutate.
- `preferredProvider` set to `or-openrouter`.
- Built-in CCR Claude-Code rules left enabled; **catalog has no Anthropic
  native models**, so the old Opus bleed requires a native Anthropic provider
  to return (still disable discovery escalation if you want belt-and-suspenders).

## Hermes relationship

Hermes itself continues to use its own providers (`config.yaml` /
OpenRouter). This package is only so **Claude Code** (often used as
Dockerfile-of-sorts coding worker next to Hermes) can swap orchestrator brains
the same way GLM already could.
