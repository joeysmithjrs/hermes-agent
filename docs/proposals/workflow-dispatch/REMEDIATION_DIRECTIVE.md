# DIRECTIVE — Remediation pass on Workflow Dispatch Phase 1

**Primary implementer for this pass:** Terra (`gpt-5.6-terra`) — you fixed nothing yet in code; grok built it, you reviewed it and found real BLOCK/HIGH bugs. Now **you fix your own findings.**
**Orchestrator:** z-ai/glm-5.2 (spawns Task agents, integrates, runs tests, commits, opens PR).
**Budget:** $15 hard. No artificial turn ceiling.
**Branch:** `feat/workflow-dispatch` (already has uncommitted `workflow/` + `tests/workflow/` on disk from the prior run — DO NOT regenerate from scratch, fix in place).

## Source of truth for what to fix

`docs/proposals/workflow-dispatch/REVIEW_NOTES.md` — read it fully first. Fix **every BLOCK and HIGH**, and the MEDIUM items if budget allows (in priority order below). Do not re-litigate PASS items.

## Fix list (priority order)

1. **BLOCK — resume reads only run.json, not checkpoint.json**
   `workflow/runtime/driver.py:623-624,646-656`, `workflow/store/checkpoint.py:12-26`
   Fix: authoritative recovery read must fall back to `checkpoint.json` if `run.json` missing/invalid (or keep them always in sync via atomic write on every checkpoint). Add a regression test that: starts a driver, executes+checkpoints one node WITHOUT finalizing (no run.json write), then calls public `resume()` and asserts (a) succeeded nodes are not re-run, (b) a `side_effects: external` running node becomes `INTERRUPTED`.

2. **BLOCK — F2 override rejection bypassed in fanout branches + resume-load skips re-verify**
   `workflow/verify.py:92-95,166-246`, `workflow/runtime/driver.py:512-523,658-665`
   Fix: `_check_node` (or equivalent) must also validate `fanout.branch.spec` / `map.branch.spec` with the SAME override/live-tool/run-allowlist/side_effects rules as top-level nodes, reporting a qualified branch node id in errors. In `resume()`, re-run `verify_ir` on the loaded `VerifiedIR.ir` using the stored/effective strict policy BEFORE constructing `Driver`; reject a tampered/altered stored definition. Add: (a) strict-mode test for nested branch overrides (tools/model/profile/max_turns/workspace) all rejected, (b) resume-tampered-definition rejection test.

3. **BLOCK — F4 script registry is a mutable global, allows os.system smuggling**
   `workflow/runtime/scripts.py:19-37`, `workflow/verify.py:213-241`, `workflow/runtime/driver.py:495-500`
   Fix: make the approved-name registry immutable after module init (no exported runtime mutator, or gate it behind an explicit test-only/bootstrap-only internal flag that is NOT part of the public API). Branch/join `run:` fields must be validated against the SAME immutable mapping — no silent fallback to `{"branch": index}` on unregistered names; must fail verification/execution instead. Add: test that external code cannot register new names post-init; test that unregistered branch `run:` is rejected in strict verification.

4. **HIGH — resumed gate run silently reports succeeded while gate still open, downstream never runs**
   `workflow/runtime/driver.py:555-561,583-595,646-693`, `workflow/__init__.py:183-192`
   Fix (Phase-1-appropriate): `resume()` of a run currently `awaiting_gate` with NO consumed decision must refuse with a clear "Phase 2: gate unpark not implemented" error — NEVER silently finalize as `succeeded` while a gate node is still pending. If minimal decision-consumption is cheap, implement it (validate signal, mark gate resolved, clear awaiting_gate, continue); otherwise the explicit refusal is acceptable. Add test: park → (no decision or decision recorded) → resume → assert either correct continuation OR explicit non-success error, and assert final status is never `succeeded` while an unresolved gate exists in the graph.

5. **HIGH — fanout branch state not persisted, unrecoverable after crash**
   `workflow/runtime/driver.py:64-104,193-194,442-462,464-510`
   Fix: persist the rendered branch leaf spec (or a safely reconstructable reference) + branch item + parent fanout id + node kind into the node-run record on disk, not just in memory. `NodeRun.from_dict()` must restore enough to resume that specific branch correctly rather than nulling `branch_node`. Add regression: crash mid-fanout (after 1 of 3 branches checkpointed), resume, assert the correct remaining branches run (not the fanout node itself, not wrong branches).

6. **MEDIUM — CLI --warn-overrides flag can't read config default (dead code path)**
   `workflow/cli.py:42,49,138-146,163-171`
   Fix: use `default=None` for the flag (not `store_true`'s implicit False) so the config fallback (`workflow.phase1_warn_overrides: true`) actually takes effect when the flag is omitted. Add test proving config-driven warn mode works without passing `--warn-overrides` explicitly, and that `hermes workflow validate docs/proposals/workflow-dispatch/minimal_workflow.yaml` succeeds under that config.

7. **MEDIUM — max_parallel_nodes accepted but unused (false advertising)**
   `workflow/runtime/driver.py:173-175,223-230,461-462,464-510`
   Fix: EITHER implement a simple bounded-concurrency cap for fanout branch execution (checkpoint-safe — do not concurrently write conflicting state) OR remove the option from config/CLI surface and document Phase 1 as strictly sequential. Pick whichever is cheaper/safer given remaining budget; document the choice in IMPLEMENTATION_LOG.md. Add a test asserting the chosen contract (either the cap is honored, or the option is absent/documented as no-op removed).

8. **MEDIUM — structural verifier gaps vs design (single entry / join>=2 / fanout requires downstream join)**
   `workflow/verify.py:76-90,233-241`
   Fix ONLY IF BUDGET REMAINS after 1-7: either enforce the design's structural rules (reject multi-entry graphs, joins with <2 upstreams, fanout without downstream join/map — removing the test that currently pins the looser behavior) OR explicitly amend `2026-07-29-workflow-dispatch-design.md` to document the looser semantics as the accepted contract. Do not leave it silently inconsistent. Prefer enforcing the stricter design contract if time allows; otherwise document + skip.

## Non-negotiables (same as before, do not violate)

- No `run_agent.py` edits.
- No permanent conversation tool registration; no `_HERMES_CORE_TOOLS` growth.
- Default-off (`workflow.enabled: false`).
- `hermes --help` must survive if the workflow package is broken/missing.
- Reuse `delegate_task` for the inherit path; don't build a second agent loop.
- Keep the diff scoped to `workflow/`, `tests/workflow/`, docs, and the existing single soft-import hunk in `hermes_cli/main.py`.

## Model routing for this pass

- **Orchestrator:** z-ai/glm-5.2
- **Primary fix implementer (Task agent):** review-terra profile / model `gpt-5.6-terra` — spawn it to write the actual code fixes for items 1-8, since it authored the findings and knows exactly what's wrong and where.
- **Support coder/tests:** code-kimi (`moonshotai/kimi-k3`) for regression test scaffolding in parallel where useful.
- **Second-pass adversarial check (optional, only if budget allows near the end):** build-grok (`x-ai/grok-4.5`) — quick re-check that Terra's fixes actually close the BLOCK findings without introducing new ones. Skip if budget is tight; a green pytest run + Terra's own confirmation is acceptable given cost constraints.
- If any model call 402/5xx: retry up to 3x with backoff; if a specific model stays down, fall back to z-ai/glm-5.2 for that subtask rather than aborting.

## Process

1. Read `REVIEW_NOTES.md` fully (already done in a prior context — do it again fresh, don't assume).
2. Fix items 1-3 (BLOCK) first. Run `pytest tests/workflow -q` after each fix. Do not proceed to HIGH items until all BLOCKs pass their new regression tests.
3. Fix items 4-5 (HIGH).
4. If budget remains, fix 6-7 (MEDIUM), then 8 if still budget.
5. Full `pytest tests/workflow -q` green.
6. Update `IMPLEMENTATION_LOG.md`: add a "Remediation pass" section listing what was fixed, what was deferred (with reason + budget), and updated F-list/acceptance table status.
7. Update or replace `REVIEW_NOTES.md` verdict line if it's now SHIP / SHIP-WITH-KNOWN-GAPS (list remaining accepted gaps explicitly — do not silently upgrade the verdict).
8. `git add` all new/modified files under `workflow/`, `tests/workflow/`, `docs/proposals/workflow-dispatch/`, and the `hermes_cli/main.py` soft-import hunk. Do NOT commit stray `cc_*.json` run artifacts or `CC_RUN_STARTED*.txt` files.
9. Commit with a clear message referencing the fixed findings.
10. Open a PR from `feat/workflow-dispatch` to `main` on `joeysmithjrs/hermes-agent` (or update the existing one if already open). **DO NOT MERGE.**
11. Print: PR URL, final pytest summary, and a short table of "fixed / deferred (budget)" for items 1-8.

## Stop condition

Stop when either (a) all 8 items are addressed and tests are green and PR is open/updated, or (b) budget is exhausted — in which case write an honest `STATUS.md` in the specs folder listing exactly which of items 1-8 got fixed vs not, before you run out.
