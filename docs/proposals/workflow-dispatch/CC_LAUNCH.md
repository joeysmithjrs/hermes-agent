# Claude Code launch kit — Workflow Dispatch Phase 1

Companion to `IMPL_DIRECTIVE.md`. Humans/Hermes use this to **spawn** the swarm.

## Preconditions

- [ ] CCR multi-model live (`python3 scripts/ccr_openrouter_models.py verify`)
- [ ] `~/.claude/settings.json` points at CCR + apiKeyHelper
- [ ] Agent templates in `~/.claude/agents/` (scout-glm, build-terra, reason-grok, …)
- [ ] Specs at `docs/proposals/workflow-dispatch/` (AUDITED)
- [ ] Branch `feat/workflow-dispatch` rebased on latest fork `main`
- [ ] GitHub auth persistent (`gh auth status`)

## Recommended launch (orchestrator = GLM)

```bash
cd /home/hermes/research/hermes-workflow-dispatch   # or fresh worktree from main
git fetch origin && git rebase origin/main

# Ensure specs present
test -f docs/proposals/workflow-dispatch/IMPL_DIRECTIVE.md

export ANTHROPIC_MODEL=z-ai/glm-5.2
export CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY=0   # avoid accidental non-allowlist models

claude --model z-ai/glm-5.2 \
  --dangerously-skip-permissions \
  --max-turns 120 \
  --max-budget-usd 25 \
  -p "$(cat docs/proposals/workflow-dispatch/IMPL_DIRECTIVE.md)

BEGIN IMPLEMENTATION NOW.

Protocol:
1) Spawn scout-glm for codebase map (read-only).
2) Spawn think-inkling only if IR/resume unclear after reading AUDIT+design.
3) Implement with build-terra / code-kimi as Task agents for parallel slices A–D.
4) Spawn reason-grok for adversarial review before PR.
5) Run pytest tests/workflow; fix until acceptance checklist green.
6) Commit on feat/workflow-dispatch; open PR to main; DO NOT MERGE.
7) Print PR URL + test summary.

Stay on allowed models only: z-ai/glm-5.2, x-ai/grok-4.5, gpt-5.6-terra,
thinkingmachines/inkling, moonshotai/kimi-k3, moonshotai/kimi-k2.7-code.
"
```

## Cheaper launch (orchestrator only, no Task fanout)

Same prompt but add: `Do not spawn Task agents; implement yourself on glm only.`

## Hermes-side babysitting checklist

After CC finishes (or mid-flight):

```bash
cd <worktree>
pytest tests/workflow -q
hermes workflow --help || true
git log --oneline origin/main..HEAD
gh pr list --head feat/workflow-dispatch
```

## Success signal

`hermes workflow validate` + FakeWorker `run` works, PR open, no `run_agent.py` in diff.
