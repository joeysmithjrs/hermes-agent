# Upstream Sync Log — `chore/sync-upstream-main`

**Date:** 2026-07-30  
**Branch:** `chore/sync-upstream-main`  
**Source:** `upstream/main` (`NousResearch/hermes-agent`)  
**Into:** fork main base `c839e3280` (post PR #1–#4)  
**Merge commit:** `897319b20 Merge remote-tracking branch 'upstream/main' into chore/sync-upstream-main`

---

## Summary

| Metric | Value |
|--------|--------|
| Upstream commits integrated | ~2900 (`origin/main..upstream/main` at merge time) |
| Fork-only commits retained | ~20 (workflow dispatch, CCR multi-model, deploy/VPS, reddit skill, prior upstream merge) |
| Real conflicts | **1** — `hermes_cli/main.py` |
| Upstream top-level `workflow/` package | **None** — no name collision |
| Operator decision needed | None for this merge |

---

## Conflict resolution

### `hermes_cli/main.py` (only conflict)

**Both sides touched `_BUILTIN_SUBCOMMANDS`.**

| Side | Change |
|------|--------|
| Upstream | Added `skin`, `sync` tokens |
| Fork | Added `workflow` token (soft-imported workflow CLI) |

**Resolution:** **merged both** — keep upstream `skin`/`sync` **and** fork `workflow`.

Not a wholesale ours/theirs pick.

---

## Preserve checks (post-merge)

| Asset | Status |
|-------|--------|
| `workflow/` package | Present (driver, verify, store, CLI) |
| `tests/workflow/` | Present |
| `docs/proposals/workflow-dispatch/` | Present |
| `scripts/ccr_openrouter_models.py` + CCR docs | Present |
| Fork deploy orchestration | `.github/workflows/deploy.yml` still present |
| Soft-import of `workflow` in CLI | Retained via merge resolution |

---

## Verification (operator-finished residual)

Before push/PR:

```
pytest tests/workflow -q  →  66 passed
import hermes_cli.main, workflow, workflow.cli  →  ok
ruff check --preview workflow hermes_cli/main.py  →  All checks passed
```

Sonnet agent had already completed the merge + claimed the same workflow suite green during the automated pass; the residual was log + push + open PR. Operator re-ran the acceptance suite above before shipping.

---

## Notable upstream surface hits (no conflict, awareness)

Upstream moved a large amount of CI/docker/docs/ACP/surface code (typical Nous main churn). External shared files landed cleanly. Nothing in the conflict list besides `main.py` subcommand set.

No evidence upstream independently shipped a parallel “workflow dispatch IR” package under the same path — our `workflow/` remains fork-owned.

---

## Downstream risks for Phase 2

After this sync lands on fork `main` and deploys, Phase 2 work (live OverrideWorker / per-node model) should re-check:

- `tools/delegate_tool.py` `_build_child_agent` / `delegate_task` signatures vs post-sync tree  
- `hermes_state` helpers still importable for the sqlite index  
- any new default CLI registration patterns around soft-imports  

Document-only note; no blockers found in the smoke above.

---

## Process notes

- Method: **real merge commit** (not rebase/squash of fork history)  
- PR to `origin/main` only after this log + local suite green  
- **Merge of PR is operator-gated** (one-shot push/merge request from operator after this file ships)
