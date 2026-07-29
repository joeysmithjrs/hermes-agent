# Workflow Dispatch — Upstream / Sync Strategy

**Companion to:** design + api specs · 2026-07-29  
**Fork:** `joeysmithjrs/hermes-agent` · worktree branch `feat/workflow-dispatch`

---

## 1. Goal

Ship workflow dispatch so **rebasing onto Nous `main` stays boring**: near-zero conflicts in sacred paths (`run_agent.py`, gateway hot loop, core toolset after session start).

---

## 2. Preferred packaging (Footprint Ladder)

| Rung | What | Phase |
|------|------|-------|
| R2 | New package `workflow/` + skill docs + CLI soft-import | MVP |
| R3 | Optional toolset `workflow` (default off) | Phase 2 |
| Not | Growing `_HERMES_CORE_TOOLS` permanently | avoided |
| Not | Mid-turn tool schema swap | forbidden |

---

## 3. NEW paths (own these; conflict-free)

```
workflow/                          # entire package — 100% new
docs/superpowers/specs/            # design artifacts
skills/.../workflow-dispatch/      # optional skill MD (agent how-to)
tests/workflow/                    # hermetic tests
```

Add only if needed:

```
website/docs/user-guide/features/workflow.md   # docs site optional
```

---

## 4. Touch points into existing tree (minimal)

| File | Change kind | Risk | Notes |
|------|-------------|------|-------|
| `hermes_cli/main.py` | **additive** soft import register_subcommands | low | `try: from workflow.cli import register` |
| `hermes_cli/commands.py` | optional slash command defs | low | only if slash surfaces wanted |
| `toolsets.py` | add `"workflow": [...]` optional set | low | not in core default list |
| `tools/registry.py` | auto-discovery if `workflow/tool.py` calls `register` | low | gate with `check_fn` on `workflow.enabled` |
| `cron/jobs.py` or scheduler | **prefer none in P1**; shell-out from script | med if touched | document wrapper recipe first |
| gateway webhook routes | optional P2 feature-flag | med | can use existing subscribe + script |
| `tools/delegate_tool.py` | P2 optional kwargs only if uncoverable otherwise | med | additive kwargs; never required |
| **`run_agent.py`** | **FORBIDDEN in P1** | high | no hot-path edits |
| `agent/prompt_builder.py` | FORBIDDEN for schema swaps | high | — |
| kanban schema / dispatcher | FORBIDDEN as foundation | high | adapter only later |

---

## 5. Forbidden / sacred paths (do not drive design through these)

- `run_agent.py` conversation loop / tool dispatch mid-turn mutation  
- Anything that rebuilds system prompt or tool list after first turn  
- Hard dependency on third-party SaaS in-tree  
- New required `HERMES_*` env vars for behavioral config (use `config.yaml`)  
- Replacing `delegate_task` or kanban

---

## 6. Sync playbook with upstream (Nous)

### 6.1 Day-to-day

```bash
git fetch upstream   # or origin if tracking Nous
git rebase upstream/main
# expect conflicts only in hermes_cli/main.py registration block if any
```

### 6.2 Conflict policy

| Path | Resolution |
|------|------------|
| `workflow/**` | always ours |
| `hermes_cli/main.py` | re-apply 5–15 line soft-import block after upstream edits |
| `toolsets.py` | re-add `workflow` toolset entry if lost |
| anything else | stop; redesign to eliminate touch |

### 6.3 Upstream PR strategy

1. Land design + tests on fork.  
2. Implement package entirely under `workflow/`.  
3. Open upstream PR as **optional feature** behind `workflow.enabled: false`.  
4. Highlight: no `run_agent.py` changes; reuses `delegate_task`.  
5. Accept maintainer asks to move CLI registration pattern to match project norms.

If upstream rejects core housing: **extract to installable plugin** calling public `hermes` CLI + subprocess agent runs — awkward but doable because IR+driver are self-contained.

---

## 7. Plugin fallback architecture

If core inclusion fails:

```
~/.hermes/plugins/workflow_dispatch/
  plugin.py          # registers CLI via plugin hooks if available
  workflow/          # same package vendored or pip extra
```

Driver still lives outside conversation tools if plugin tool hooks are weak; CLI + cron remain primary.

---

## 8. Config / secrets boundary

| Kind | Where |
|------|--------|
| `workflow.enabled`, budgets, parallel | `config.yaml` |
| webhook secrets | `.env` / webhook secret store |
| per-run artifacts | `$HERMES_HOME/workflows/` (gitignore-ish operational data) |

---

## 9. Versioning

- IR `version` field; driver supports N and N-1.  
- Reject future major IR without migration note.  
- Package follows Hermes release; feature flag defaults off until stable.

---

## 10. Review checklist for fork PRs

- [ ] No edits under `run_agent.py`  
- [ ] No default-on toolset injection into core bundle  
- [ ] Soft-import CLI survives `workflow` package missing  
- [ ] Tests hermetic (tmp HERMES_HOME)  
- [ ] Design still matches PM pipeline mapping table  
- [ ] `git diff upstream/main --stat` shows almost all bytes in `workflow/` + docs/tests  

---

## 11. Mapping to current install

| Machine path | Role |
|--------------|------|
| `/opt/hermes-agent` | live install (`origin` = `joeysmithjrs/hermes-agent`) |
| `/home/hermes/research/hermes-workflow-dispatch` | worktree `feat/workflow-dispatch` for this work |
| Specs | `docs/superpowers/specs/*` in worktree |

Implementation should occur in the worktree, not by haphazardly editing only `/opt` without branch discipline.
