# Workflow Dispatch — Adversarial Review Notes

## Verdict

**SHIP-WITH-KNOWN-GAPS** — a remediation pass (2026-07-30) closed every BLOCK
and HIGH finding and both MEDIUM findings; the hermetic suite is now
**59 passed** (`python -m pytest tests/workflow -q`). The crash-resume
contract works against a real checkpoint window (not a fabricated `run.json`),
F2/F4 override + allowlist rules apply to fanout branch templates and are
re-verified at resume-load, the script registry is frozen after bootstrap,
fanout branch state is persisted and resumable, a resumed gated run never
silently finalizes as `succeeded`, the CLI `phase1_warn_overrides` config
fallback is reachable by default, and `max_parallel_nodes` is documented as a
reserved no-op with Phase 1 strictly sequential. See
`IMPLEMENTATION_LOG.md` "Remediation pass" for the per-item table and the
regression tests that pin each fix.

Remaining **accepted gaps** (all explicitly Phase 2, none silently upgraded):

- **Gate runtime unpark** — `decide_gate` records the decision but the driver
  does not consume it; a resumed gated run stays `awaiting_gate` (never
  `succeeded` while a gate is pending). Full unpark is Phase 2.
- **Override enforcement path** — per-node `model`/`tools`/`profile`/
  `max_turns`/`workspace` are rejected/warned (never silently honored); true
  subprocess/profile isolation is Phase 2.
- **Bounded concurrency** — `max_parallel_nodes` is a reserved no-op; Phase 1
  is strictly sequential (introducing threading would conflict with the
  per-step checkpoint guarantee).
- **Structural strictness** — multi-entry graphs, joins with a single
  fanout/map upstream (≥2 branches), and terminal fanout are accepted Phase-1
  semantics, documented in design §5 "Phase 1 implementation note" (the
  stricter single-entry / join-≥2 / fanout-requires-downstream-join rules are
  deferred). This is a documented contract, not a silent inconsistency.
- Phase-2/3 debts unchanged from the original log: conversation tool
  registration, Python DSL, runtime output-schema validation, richer reducers
  (`first_k`/`majority`), native cron/webhook trigger fields, secret-tool
  taxonomy, OS-level script isolation, budget-gate continuation, and live
  standalone CLI execution (requires a parent-agent/shim).

The detailed F-list / acceptance table below is retained as the original
review record; its BLOCK/HIGH/MEDIUM rows were addressed by the remediation
pass above.

## F-list audit

| Finding | Result | Evidence |
|---|---|---|
| F1 — node bodies keyed by `node_run_id` | PASS | `/home/hermes/research/hermes-workflow-dispatch/workflow/store/fs.py:58-67,133-137` constructs node paths from `node_run_id`; `/home/hermes/research/hermes-workflow-dispatch/workflow/runtime/driver.py:247-264,445-453,509-510` creates unique branch IDs and writes each output. Existing `test_f1_fanout_branches_distinct_node_run_id_paths` passes. |
| F2 — strict Phase-1 override rejection, reverified on load | FAIL | Top-level agents are checked in `/home/hermes/research/hermes-workflow-dispatch/workflow/verify.py:175-211`, but `fanout.branch` is never converted and passed through `_check_node`; a strict workflow with branch `tools`, `model`, `profile`, `max_turns`, and `workspace` compiled and ran. Resume deserializes stored `VerifiedIR` at `/home/hermes/research/hermes-workflow-dispatch/workflow/runtime/driver.py:658-665` without calling `verify_ir`, so a modified stored definition is trusted. |
| F4 — registry-only `run:` resolution | FAIL | Normal script and join checks use `is_registered` (`/home/hermes/research/hermes-workflow-dispatch/workflow/verify.py:213-241`), but `/home/hermes/research/hermes-workflow-dispatch/workflow/runtime/scripts.py:19-23` exports an unrestricted mutable `register()` API. A caller can run `scripts.register("os.system", os.system)` then compile and execute `run: os.system`. Fanout branch scripts also bypass verification and execute any registered name or silently return a fake result (`driver.py:495-500`). |
| F5 — required cap and pre-spawn cardinality | PASS | `/home/hermes/research/hermes-workflow-dispatch/workflow/verify.py:222-231` requires a positive cap. `/home/hermes/research/hermes-workflow-dispatch/workflow/runtime/driver.py:430-440` rejects `len(branches) > max_branches` before the spawn loop at :445. Boundary `len == max_branches` succeeded in an independent smoke probe. |
| F6 — external running node interrupted, safe node requeued | PARTIAL / FAIL for crash-resume acceptance | The in-memory resume policy correctly marks a top-level external node failed at `/home/hermes/research/hermes-workflow-dispatch/workflow/runtime/driver.py:670-681`; existing simulated-state test passes. But actual per-step state is written only to `checkpoint.json` at :623-624, whereas `resume()` reads only `run.json` at :653-656. A kill after a checkpoint and before finalization makes resume raise `FileNotFoundError`; this violates the required “Resume after kill” behavior. Fanout branch executions are also not faithfully resumable: branch node template/item are intentionally transient and are discarded in `NodeRun.from_dict` (`driver.py:87-104,193-194`). |
| F7 — gate status enums and parking | PASS for stated Phase-1 enum/parking scope | `/home/hermes/research/hermes-workflow-dispatch/workflow/ir.py:42-52` includes both states; `/home/hermes/research/hermes-workflow-dispatch/workflow/runtime/driver.py:555-561` parks a run as `awaiting_gate`. Full unpark is explicitly a Phase-2 stub. |
| F8 — dual-control cannot auto-approve | PASS | `/home/hermes/research/hermes-workflow-dispatch/workflow/verify.py:96-108` hard-rejects `approve_auto` with `dual_control: true`; `test_f8_approve_auto_with_dual_control_rejected` passes. |
| F11 — reuse SQLite hardening | PASS | `/home/hermes/research/hermes-workflow-dispatch/workflow/store/index.py:20-25,129-140` imports the `hermes_state` helpers and calls `apply_wal_with_fallback`. `test_f11_sqlite_index_reuses_hermes_state_helpers` confirms object identity. The two private imports are dead imports, but the operational helper is genuinely reused. |

## Non-negotiables

| Requirement | Result | Evidence |
|---|---|---|
| Deterministic code control flow, not an LLM loop | PASS | `/home/hermes/research/hermes-workflow-dispatch/workflow/runtime/driver.py:198-236` is a deterministic ready-set loop; only agent leaves call the injected worker. |
| Zero edits to `run_agent.py` and `agent/prompt_builder.py` | PASS | `git diff origin/main --stat -- run_agent.py agent/prompt_builder.py` was empty. |
| No permanent conversation tool / `_HERMES_CORE_TOOLS` addition / schema mutation | PASS | `git diff origin/main --name-only` contains neither toolset nor agent/core files. No workflow registration appears in the core toolset diff. |
| Default-off, CLI run refuses disabled workflows | PASS | Defaults set `enabled: False` at `/home/hermes/research/hermes-workflow-dispatch/workflow/config.py:14-23`; CLI refusal is `/home/hermes/research/hermes-workflow-dispatch/workflow/cli.py:192-196`. `test_disabled_run_refuses_nonzero` passes. |
| Top-level package + soft import (Footprint Ladder rung 2) | PASS | New top-level package exists. The only tracked core-file diff is `/home/hermes/research/hermes-workflow-dispatch/hermes_cli/main.py`, with one built-in-subcommand token addition and one guarded registration hunk. |
| `delegate_task` inherit-path reuse; no second agent loop | PASS, with operational limitation | `/home/hermes/research/hermes-workflow-dispatch/workflow/runtime/worker.py:83-102` calls `tools.delegate_tool.delegate_task(goal=..., context=..., role="leaf", parent_agent=...)`; standalone live CLI has no parent agent and therefore fails deliberately at :87-91. |
| Acyclic v1 | PASS | `/home/hermes/research/hermes-workflow-dispatch/workflow/verify.py:73-74,127-148` detects cycles. |
| Hermetic test suite | PASS | `python -m pytest tests/workflow/ -q` completed with `48 passed`. |

## Acceptance checklist

| Checklist item | Result | Evidence |
|---|---|---|
| `pytest tests/workflow -q` green hermetic | PASS | Re-run: `48 passed in 1.17s`. |
| Linear YAML end-to-end FakeWorker succeeds | PASS | `tests/workflow/test_driver.py:test_linear_three_node_success`. |
| Fanout 3 → join → three distinct output paths | PASS | `tests/workflow/test_driver.py:test_f1_fanout_concat_three_branch_outputs`; actual writes at `driver.py:445-510`. |
| Resume after kill; no succeeded double-finalize; external becomes INTERRUPTED | FAIL | Test simulates a handcrafted `run.json`, not an actual kill. Real checkpoint-only crash cannot resume because `resume()` reads `run.json`, not `checkpoint.json` (`driver.py:623-624,653-656`). |
| Oversize fanout → `CARDINALITY`, no spawn | PASS | `tests/workflow/test_driver.py:test_f5_oversize_fanout_cardinality_no_overspawn`; cap check precedes spawning. |
| Verifier rejects override-only fields | FAIL | Only top-level agents are tested. Nested `fanout.branch.spec` overrides compile in strict mode; no regression test exists. |
| Verifier rejects unregistered `run:` | PARTIAL / FAIL | Static unregistered names are rejected (`test_f4_run_not_in_allowlist_rejected`), but exported `register()` permits runtime registry pollution with `os.system`; branch custom scripts are not validated. |
| Reject `approve_auto` + `dual_control:true` | PASS | `tests/workflow/test_verify.py:test_f8_approve_auto_with_dual_control_rejected`. |
| `hermes workflow validate minimal_workflow.yaml` works | FAIL as documented acceptance wording | Default CLI invocation fails because argparse sets `warn_overrides=False`, preventing `/workflow/config.py`'s configured warn flag from being used; the command succeeds only when explicit `--warn-overrides` is supplied. Independently reproduced with config `phase1_warn_overrides: true`: exit 2. See `workflow/cli.py:42,138-146`. |
| `hermes --help` survives missing/broken workflow package | CANNOT-VERIFY behaviorally; structural guard PASS | `tests/workflow/test_cli.py:test_soft_import_hunk_in_main_is_guarded` checks source text only. The hunk in `/home/hermes/research/hermes-workflow-dispatch/hermes_cli/main.py` is correctly inside `try/except`, but no subprocess test exercises a broken import. |
| Diff scoped to workflow/tests/docs plus soft import | PASS for tracked files, with staging caveat | `git diff origin/main --stat` reports proposal docs plus the 16-line `hermes_cli/main.py` change. New `workflow/`, `tests/workflow/`, and log are untracked, so ordinary diff stat underreports them until staged. No tracked sprawl into `agent/`, `tools/`, `cron/`, `gateway/`, or `toolsets.py`. |
| Zero `run_agent.py` changes | PASS | `git diff origin/main -- run_agent.py` is empty. |

## Bugs / gaps found

### BLOCK — checkpointed resume does not resume an actual interrupted run

- **Location:** `/home/hermes/research/hermes-workflow-dispatch/workflow/runtime/driver.py:623-624,646-656`; `/home/hermes/research/hermes-workflow-dispatch/workflow/store/checkpoint.py:12-26`.
- **Failure scenario:** The driver checkpoints after node A at `checkpoint.json`. A process kill occurs before `_finalize()` writes `run.json`. `workflow.resume(run_id)` calls `load_run_record()`, which reads only `run.json`, sees `{}`, and raises `FileNotFoundError`. This is the exact crash window the checkpoint design and acceptance item claim to handle. The current F6 test manually puts state in `run.json`; it does not test the real path.
- **Minimal fix:** Define a single authoritative recovery read: load `run.json` if present and valid, otherwise `checkpoint.json` (or atomically update the authoritative record at every checkpoint). Preserve existing finalization behavior. Add a process-independent regression that starts a driver, executes/checkpoints a node without finalizing, then calls public `resume()` and proves succeeded nodes are not re-run and external `running` becomes `INTERRUPTED`.

### BLOCK — F2 override policy is bypassed in fanout branches and at resume load

- **Location:** `/home/hermes/research/hermes-workflow-dispatch/workflow/verify.py:92-95,166-246`; `/home/hermes/research/hermes-workflow-dispatch/workflow/runtime/driver.py:512-523,658-665`.
- **Failure scenario:** A strict workflow can put `tools: [exec]`, `model`, `profile`, `max_turns`, and `workspace` under `fanout.branch.spec`. The verifier only calls `_check_node` for top-level `ir.nodes`; it never validates the nested node. Independent strict compile/run succeeded. Separately, alter `definitions/<workflow>.json` between run and resume: `resume()` constructs `VerifiedIR.from_dict()` and executes it without `verify_ir`, despite the design’s “re-verified at load” rule.
- **Minimal fix:** Materialize/validate branch templates with the same node-kind, prompt, override, live-tool, `run:`, and `side_effects` rules as top-level nodes, reporting the fanout node ID or a qualified branch ID. In `resume()`, re-run `verify_ir` on `vir.ir` using the stored/configured strict policy before constructing `Driver`; reject a tampered definition. Add strict-mode tests for every nested override and a resume-tampered-definition rejection test.

### BLOCK — F4 is a mutable global allowlist, not an allowlisted registry

- **Location:** `/home/hermes/research/hermes-workflow-dispatch/workflow/runtime/scripts.py:19-37`; `/home/hermes/research/hermes-workflow-dispatch/workflow/verify.py:213-241`; `/home/hermes/research/hermes-workflow-dispatch/workflow/runtime/driver.py:495-500`.
- **Failure scenario:** Any in-process plugin/import can call the public `scripts.register("os.system", os.system)`. The verifier then accepts `run: os.system` and the driver invokes it. This is arbitrary shell execution via a YAML field after trivial registry pollution, precisely the threat F4 rejects. Custom `run:` inside a fanout branch has no verifier check; if unregistered it silently returns `{"branch": index}` rather than failing.
- **Minimal fix:** Make the Phase-1 registry immutable after module initialization (private bootstrap only, no exported runtime mutator), or require a verifier-owned immutable mapping of approved symbolic names. Validate branch script/join templates against that same mapping and fail execution if a branch `run:` is unregistered. Add tests that attempted registration cannot alter accepted names and that branch `run: os.system` is rejected in strict verification.

### HIGH — resumed gate run reports `succeeded` while still awaiting the gate and leaves downstream unexecuted

- **Location:** `/home/hermes/research/hermes-workflow-dispatch/workflow/runtime/driver.py:555-561,583-595,646-693`; `/home/hermes/research/hermes-workflow-dispatch/workflow/__init__.py:183-192`.
- **Failure scenario:** Run A → gate G → B. It parks as `awaiting_gate`; record an `approve` signal; call `resume()`. Resume unconditionally changes run status to `running`, does not inspect the decision, has G still `awaiting_gate`, finds no ready work, and `_finalize()` changes status to `succeeded` because there are no failures. `awaiting_gate` remains `g` and B never runs. This is contradictory persisted state and unsafe operator feedback.
- **Minimal fix:** Until Phase-2 unpark exists, reject `resume()` of an awaiting gate with a clear Phase-2 error and never finalize it as succeeded. Preferably implement decision consumption: validate signal/gate identity, mark gate succeeded/skipped per decision, clear `awaiting_gate`, then continue. Add park → decision → resume tests for either explicit refusal or correct continuation; assert no terminal success with a non-null pending gate.

### HIGH — fanout branches are not recoverable from persisted state

- **Location:** `/home/hermes/research/hermes-workflow-dispatch/workflow/runtime/driver.py:64-104,193-194,442-462,464-510`.
- **Failure scenario:** Branch work is created from a transient `branch_node` and `_branch_items`; neither is serialized. `NodeRun.from_dict()` explicitly sets `branch_node=None`. A crash after fanout materialization, or during an individual branch, loses the branch template and input. Resume may treat branch runs as node ID `fanout_id`, resolve that to the fanout instead of its actual branch leaf, and cannot render the original branch item. This invalidates fanout resumability and can run the wrong work after a crash.
- **Minimal fix:** Persist sufficient materialized branch state: the immutable rendered branch leaf spec (or safely reconstructable template), branch item, parent fanout ID, and accurate execution kind. On load, restore it and make ready/resume operate on that leaf. Add interrupted-fanout regression coverage for safe branch requeue and external branch interruption.

### MEDIUM — CLI config warn flag is unreachable by default

- **Location:** `/home/hermes/research/hermes-workflow-dispatch/workflow/cli.py:42,49,138-146,163-171`.
- **Failure scenario:** `argparse` `store_true` always supplies `False`, not `None`. Therefore the `if warn is None` config fallback is dead. With `workflow.phase1_warn_overrides: true`, `hermes workflow validate minimal_workflow.yaml` still strict-rejects its documented `tools` fields unless the user explicitly passes `--warn-overrides`.
- **Minimal fix:** Use `default=None` for these flags, or use a mutually exclusive `--warn-overrides`/`--strict-overrides` policy. Add subprocess/direct parser coverage proving config-driven warn mode works and the required minimal YAML command is accepted under its documented configuration.

### MEDIUM — advertised parallelism is not implemented

- **Location:** `/home/hermes/research/hermes-workflow-dispatch/workflow/runtime/driver.py:173-175,223-230,461-462,464-510`.
- **Failure scenario:** `max_parallel_nodes` is accepted/configured but never read after initialization. Nodes and branches run sequentially. This is not an F-list violation, but it is an untruthful operational setting and conflicts with the configuration contract.
- **Minimal fix:** Either implement bounded concurrency with checkpoint-safe state transitions, or remove/defer the option and document deterministic sequential Phase 1. Add a test that verifies the chosen contract instead of silently accepting a no-op cap.

### MEDIUM — verifier/design structure requirements are only partially implemented

- **Location:** `/home/hermes/research/hermes-workflow-dispatch/workflow/verify.py:76-90,233-241`; `/home/hermes/research/hermes-workflow-dispatch/docs/proposals/workflow-dispatch/2026-07-29-workflow-dispatch-design.md:400-410`.
- **Failure scenario:** The design says single entry, join has at least two upstreams, and fanout has a downstream join/map. The code permits multiple entry nodes, a one-input join, and terminal fanout; `test_fanout_without_downstream_join_is_accepted` explicitly pins the latter. This is a spec-to-code gap, not an honest deferral marker.
- **Minimal fix:** Enforce the design’s structural rules or amend the authority docs and acceptance language to explicitly select the looser semantics. Add tests for the selected contract.

## Footprint check

Tracked diff is properly narrow: proposal docs plus `/home/hermes/research/hermes-workflow-dispatch/hermes_cli/main.py` (one soft-import registration block and `_BUILTIN_SUBCOMMANDS` entry). `run_agent.py` and `agent/prompt_builder.py` diffs are empty, and no tracked changes land in `agent/`, `tools/`, `cron/`, `gateway/`, or `toolsets.py`.

Caveat: all implementation and test files are still untracked, so `git diff origin/main --stat` materially underreports the intended package until staging. The unrelated untracked `cc_*.json` and `CC_RUN_STARTED*.txt` artifacts must not be committed.

## Residual debt honestly Phase 2

Cleanly marked / acceptable Phase-2 deferrals:

- Gate decision recording exists, and full unpark is explicitly marked Phase 2 in `/home/hermes/research/hermes-workflow-dispatch/workflow/__init__.py:183-192` and CLI output. The current broken resume behavior is not acceptable as a stub; fix it as above.
- Override subprocess/profile enforcement is openly deferred; strict rejection is the intended Phase-1 protection. It must be extended to branch templates and resume-load validation before this claim is true.
- Conversation tools/toolset registration are absent, correctly keeping Phase 2 default-off.
- DSL is explicitly stubbed.
- Model-ID registry, runtime output-schema validation, richer reducers, cron/webhook-native triggers, secret-tool taxonomy, OS isolation, and actual budget gate continuation are candidly logged Phase-2/3 debts.
- `DelegateWorker` reuse is real but the standalone non-Fake CLI cannot execute live agent workflows without a parent-agent/shim. This needs a clear user-facing Phase-1 limitation, not a claim that CLI live execution is operational.
