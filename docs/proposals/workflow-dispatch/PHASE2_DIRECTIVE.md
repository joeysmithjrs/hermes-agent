# DIRECTIVE — Workflow Dispatch Phase 2: complete implementation + validation

**Orchestrator:** you — Claude Code **Opus** (`--model opus`) on Joe's Claude Pro OAuth.
**Subagents:** spawn Task agents with model **sonnet** (`impl-sonnet`, `test-sonnet`,
`review-sonnet` under `~/.claude-pro/agents/`). Prefer parallel sonnet workers for
coding vs tests vs review; you integrate, make final architecture calls, open PR.

**Branch / worktree:** `feat/workflow-dispatch-phase2` @
`/home/hermes/research/hermes-workflow-phase2` (already checked out from
`origin/main` @ post-upstream-sync `70e1a7e06`).

**Repo:** `joeysmithjrs/hermes-agent` fork only. Open PR to `main`. **DO NOT MERGE.**

**Git identity already set:** Joe Smith / `17200287+joeysmithjrs@users.noreply.github.com`

---

## 0. Mission (one sentence)

Ship **working Phase 2** so a YAML workflow can run **live agent nodes** (not FakeWorker)
with **parent-model inheritance by default** and **optional per-node model/provider**,
**gate unpark after human approve**, and **real success/fail/gate notifications** —
end-to-end tested — without breaking Phase 1 crash-safety or upstream Footprint Ladder
rules.

---

## 1. Non-negotiables

1. **Model semantics (absolute necessity)**  
   - No `spec.model` → inherit the **parent/runtime agent model+provider credentials**.  
   - `spec.model` set → that node only runs on the overridden model (and optional
     `spec.provider` if needed for cross-provider routing).  
   - Log `effective_model` / `effective_provider` into node events/output metadata.  
   - Implementation must use Hermes's **existing** child construction path
     (`tools/delegate_tool.py` → `_build_child_agent(..., model=..., override_provider=..., …)`
     where `effective_model = model or parent_agent.model`).  
   - Do **not** invent a second agent loop. Prefer extracting a small stable helper if
     calling private `_build_child_agent` is too brittle (additive, collaborate with
     existing patterns).

2. **No silent FakeWorker**  
   - `hermes workflow run` must **never** quietly succeed agent nodes without a real
     worker when live mode is intended.  
   - FakeWorker only when `HERMES_WORKFLOW_FAKE=1` **or** explicit `--fake` flag.  
   - Otherwise construct a live worker; if impossible (no credentials/parent),
     **fail loud** with a clear error.

3. **Do not break Phase 1**  
   - Conditional edges, skip propagation, checkpoint-before-blocking-call, F4 frozen
     script registry, F1 `node_run_id` store, CARDINALITY, external INTERRUPTED —
     all stay green.  
   - Full `pytest tests/workflow -q` must remain green + new Phase 2 tests.

4. **Footprint ladder**  
   - Prefer new code under `workflow/`.  
   - Soft-import CLI already exists.  
   - Conversation tools default **off**, registered only if `workflow.tool_enabled` /
     equivalent, at **session start** only (no mid-turn toolset mutation).  
   - Avoid `run_agent.py` hot-path edits. If truly unavoidable for tool registration,
     minimize and document; prefer registry/`toolsets.py` optional toolset pattern
     used elsewhere in Hermes.

5. **Config belongs in config.yaml**, not new `HERMES_*` behavior envs (except existing
   `HERMES_WORKFLOW_FAKE` and existing `HERMES_HOME`). Use `hermes_cli.config.load_config()`
   only — no raw `yaml.safe_load` of config.yaml (guard test).

6. **Windows footguns** — any Path I/O needs `encoding="utf-8"`.

---

## 2. Phase 2 scope (implement ALL of these)

### A. Live OverrideWorker / Runtime parent (MUST)

| Deliverable | Spec |
|-------------|------|
| `RuntimeParent` / synth parent AIAgent | Built from current Hermes runtime credentials (same resolve path CLI/gateway uses). Enough for `_build_child_agent` / child run. |
| `DelegateWorker` or rename `LiveWorker` | Calls child agent construction **per node** with inherit-or-override model. Pass rendered prompt/context. Capture summary/output → node envelope. |
| CLI `run` wiring | If fake flag/env → FakeWorker; else LiveWorker. Print clearly which path. |
| Verifier Phase 2 | Allow `spec.model` / optional `spec.provider` when phase2 overrides enabled (config `workflow.phase2_overrides: true` **or** always allow model now that runtime exists — prefer **allow model when live path available**; reverse Phase 1 hard-reject of model when override path is real). Keep still-rejecting tools/profile if not yet enforced unless you implement tools narrowing too. |
| Tools (stretch in same PR if time) | Prefer: optional `spec.tools` as **subset** of parent valid tools, else ignore-with-warn. Do not silently claim isolation you don't enforce. |
| Tests | Inherit default unit; override unit with Fake/mocks of construction kwargs; no network required for unit. Optional live smoke gated/skip without API key. |

### B. Gate unpark (MUST)

| Deliverable | Spec |
|-------------|------|
| On `resume(run_id)` | If status `awaiting_gate` and gate signal file has decision: |
| `approve` | Mark gate node succeeded on `approve` port; clear awaiting; continue downstream. |
| `shelve` | Skip exec downstream (or mark skipped), finalize run appropriately. |
| No decision | Stay `awaiting_gate` — **never** become `succeeded` with open gate (already partially true). |
| CLI `hermes workflow gate` | Already records decisions; update messaging — remove "Phase 2 not implemented" when unpark works. |
| Tests | park → gate命令 approve → resume → next node runs; shelve skips. |

### C. Notifications (MUST)

| Deliverable | Spec |
|-------------|------|
| Hook | After finalize / when entering `awaiting_gate` / on terminal `failed`/`succeeded`/`partial` |
| Deliver | Reuse Hermes gateway delivery if available (`gateway.delivery` or existing send path used by other code). If gateway unavailable, best-effort log + skip without crashing run. |
| Config | Honor workflow-level `notify:` and gate-level notify when present. Reasonable defaults when true/simple. |
| Tests | Mock delivery; assert hook fired with right status. |

### D. Surfaces (SHIP what's high leverage)

| Deliverable | Spec |
|-------------|------|
| Cron recipe | Document + optional tiny example script under `docs/proposals/workflow-dispatch/` that sets `no_agent`+script calling `hermes workflow run`. Native trigger field optional if cheap. |
| Conversation toolset | Optional default-off: e.g. `workflow_run`, `workflow_status` (and gate if clean). Use `toolsets.py` + `check_fn` against `workflow.tool_enabled`. |
| Webhook | Prefer document recipe using existing webhook → CLI/script. Full HMAC route only if natural. |

### E. Hardening (MUST finish latent Phase 1 debts if quick)

| Item | Spec |
|------|------|
| Failed-upstream cascade | Downstream of a **failed** node should not hang `pending` forever — mark skip or fail with clear policy; document + test. |
| Budget pause | If `max_budget_usd` tripped → status `paused` or failed with code; resume policy documented. |
| Output schema | Optional: if NodeSpec.output schema present, validate on succeed; fail node on mismatch. |

### F. Docs

Update:
- `docs/proposals/workflow-dispatch/2026-07-29-workflow-dispatch-phases.md` Phase 2 → done/partial with honesty  
- `IMPLEMENTATION_LOG.md` Phase 2 section  
- examples YAML: live multi-model sketch (if only mockable in CI, still validate YAML)

---

## 3. Validation (acceptance checklist — ALL required before PR)

```
[ ] pytest tests/workflow -q  → green (Phase 1 + Phase 2 new)
[ ] hermes workflow validate on an example YAML with one model override node
[ ] Live CLI path (when OPENROUTER/Anthropic keys exist in env OR
    documented skip): at least one agent-node run with Fake=false either:
      (a) real live call succeeding, OR
      (b) constructive integration test with stub child that records model kwarg
[ ] Explicit unit/integration proof: no model field → inherits parent model id
[ ] Explicit proof: model field → construction received override
[ ] Gate park → approve → resume continues past gate  
[ ] Gate park → shelve → exec skipped / not double-run
[ ] Notify hooks unit-tested with mock
[ ] No silent FakeWorker without HERMES_WORKFLOW_FAKE/--fake
[ ] ruff PLW1514 / windows footgun clean on touched files
[ ] git diff does not touch forbidden sacred paths except justified (list in PR)
[ ] PR open on joeysmithjrs/hermes-agent, NOT merged
```

Spend Pro session tokens freely to **finish acceptance** — prefer completeness over half-shipped limbs.

---

## 4. Implementation order

1. Scout: read `workflow/runtime/worker.py`, `workflow/cli.py`, `workflow/__init__.py`,
   `tools/delegate_tool.py` `_build_child_agent` / credential resolve, `hermes_cli` runtime
   provider helpers, gate code paths.  
2. Sonnet impl: LiveWorker + CLI no silent fake + model inherit/override.  
3. Sonnet impl: gate unpark.  
4. Sonnet impl: notify hooks.  
5. Sonnet tests: track acceptance checklist.  
6. Failed-cascade + budget polish.  
7. Optional tool surface if time.  
8. Sonnet review-adversarial pass on silent-fake / model inherit.  
9. You (Opus): fix remaining, docs, commit, push, `gh pr create`, print URL + checklist.

---

## 5. Security reminders

- Script `run:` stay allowlisted/frozen.  
- Dual-control still rejects `approve_auto` with `dual_control: true`.  
- Live tools still need `side_effects: external` where already required.  
- Checkpoint **before** blocking live agent calls stays.

---

## 6. Stop condition

Stop when acceptance checklist is green, PR is open with body listing checklist + residual
honest Phase 3 debts (map, kanban, richer reducers).  
If something is blocked by missing secrets for **true** Live multi-provider e2e, ship
mock proofs for inherit/override + document one manual live smoke command for the operator.

BEGIN NOW. Opus orchestrates; Sonnet subagents implement/tests/review.
