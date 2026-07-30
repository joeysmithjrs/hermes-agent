# DIRECTIVE — Remediation pass 2: checkpoint-before-blocking-call durability bug

**Primary implementer:** Terra (`gpt-5.6-terra`) — same reviewer/fixer as pass 1.
**Orchestrator:** z-ai/glm-5.2.
**Budget:** $15 hard. No artificial turn ceiling.
**Branch:** `feat/workflow-dispatch-2` — create fresh off current `main` (pass 1 already merged as PR #2 / commit `d6daab4e`). Do NOT reuse the old worktree state; `main` already has the Phase-1 package + remediation-pass-1 fixes.

## Context — what's already fixed (do not re-litigate)

Pass 1 (merged) closed: F1 (node_run_id store keys), F2 (nested fanout-branch override rejection + resume re-verify), F4 (frozen script registry), gate-resume-stays-parked (HIGH), fanout branch state persistence (HIGH), `--warn-overrides` config fallback (MEDIUM), `max_parallel_nodes` documented no-op (MEDIUM), structural verifier contract documented (MEDIUM).

All of the above were **independently re-verified live** by the operator (Joe) on the merged `main` code with real process kills and adversarial YAML — not just pytest — and held up correctly:
- Oversized fanout: zero overspawn, clean CARDINALITY reject.
- F4 `scripts.register()` raises `RuntimeError` at runtime — cannot smuggle `os.system`.
- F2 nested `fanout.branch.spec` override (`model`, `tools`) correctly rejected by validate.
- Gate resume with no decision recorded: correctly stays `awaiting_gate`, never falsely reports `succeeded`.

**Do not touch or "improve" any of the above unless a fix below requires it.** They are confirmed working.

## The new BLOCK finding (this pass's job)

**Location:** `workflow/runtime/driver.py`, main run loop (~line 236-243) + `_run_node_run` (~line 331-360) + `_checkpoint`/`commit_checkpoint` (~line 665).

**Root cause:** `nr.status = "running"` is set **in memory only** inside `_run_node_run` before dispatching to `_run_agent`/`_run_script`/`_run_fanout`/`_run_join`/`_run_gate`. The actual `self._checkpoint()` call (which persists state to `checkpoint.json` + `run.json` via `commit_checkpoint`) only happens in the **main loop**, **after** `_run_node_run()` fully returns:

```python
for nr in ready:
    self._run_node_run(nr)   # <- blocks here, e.g. worker.run_node() takes real wall-clock time
    self._checkpoint()        # <- "running" status only written to disk AFTER the blocking call returns
```

**Reproduced live by the operator** with a real OS-level kill (not a simulated/hand-crafted state file):
1. Started a workflow with a `side_effects: external` agent node using a `FakeWorker(sleep_s=8.0)` to simulate a real multi-second live-model call.
2. `kill -9`'d the actual process 3 seconds in, while that node was genuinely mid-execution (blocked inside `worker.run_node()`).
3. Inspected `run.json` on disk after the kill: the node that was killed mid-flight was **not marked `running`** — because the checkpoint write never happened before the process died. It simply wasn't in the state at all yet from disk's perspective.
4. Called `resume()` on that run.
5. **The node re-ran from scratch and its side effect fired again** — the node was never durably marked "in flight," so the F6 `side_effects: external` → `INTERRUPTED`-on-resume protection (correctly implemented in pass 1) never had a chance to trigger, because it only checks nodes whose *persisted* status is `"running"` at resume time, and this node's `"running"` transition never reached disk.

**Why pass 1's regression test missed this:** the existing F6 regression test hand-constructs a `run.json`/state dict with a node already marked `"running"`, then calls `resume()` and asserts the F6 policy applies. That correctly tests the **read/resume-decision** path. It does **not** exercise the **write** path — i.e., it never proves that a real blocking call in `_run_node_run` actually gets its `"running"` transition onto disk *before* the call can hang or crash. This is a "test passed, bug still present" gap: the fix must add a regression test that exercises a REAL blocking call (a worker with `sleep_s` or an injected slow callable) racing an interrupt, not just a hand-crafted state dict.

## Required fix (do this, in order)

### Fix 2.1 — checkpoint the "running" transition before the blocking call, not after

In `_run_node_run` (or the main loop, whichever is architecturally cleaner given the existing checkpoint helper), persist `nr.status = "running"` (plus `started_at`, `node_run_id`, `node_id`, `kind`) to disk via the **existing** `commit_checkpoint`/`_checkpoint` machinery **before** calling into `_run_agent`/`_run_script`/`_run_fanout`/`_run_join`/`_run_gate`. Concretely: split the current single post-loop `self._checkpoint()` into (a) a checkpoint immediately after setting `nr.status = "running"` and before dispatch, and (b) the existing checkpoint after the node completes (success/fail). Both must go through the same atomic-write path (temp file + `os.replace`) already used elsewhere — do not add a second checkpoint mechanism.

Apply this uniformly across all node kinds that call into a worker or otherwise block for real wall-clock time: `agent` (`_run_agent`), `fanout`/`map` branches (`_run_one_branch`), and any `script`/`join`/`gate` path that could plausibly block (scripts are currently fast/synchronous built-ins, but keep the same discipline so future `run:` allowlist additions inherit the safety property for free — do not special-case by node kind if a uniform "checkpoint before dispatch, checkpoint after completion" wrapper in `_run_node_run` is cheaper and equally correct; prefer the uniform wrapper).

### Fix 2.2 — fanout branches must get the same before-dispatch checkpoint

`_run_one_branch` (line ~492) executes individual fanout branches. Verify it goes through the same `_run_node_run` path (or apply the identical before/after checkpoint discipline directly) so a crash mid-branch-execution is equally durable — this is the branch-level analog of Fix 2.1 and must not be skipped just because pass 1 already fixed *branch state persistence* (that fix was about the branch's *template/input* surviving a restart, not about the *in-flight status* being durable — these are two different bugs, both real, only the first was fixed).

### Fix 2.3 — regression test with a REAL blocking call + REAL interrupt

Add a new test (not a modification of the existing hand-crafted-state F6 test — keep that one, it's still valid for the read path) that:

1. Runs a workflow in a **separate OS process** (subprocess, not just a thread — threads can share GIL/memory state in ways that mask this class of bug) with a `FakeWorker(sleep_s=<a few seconds>)` on a `side_effects: external` agent node.
2. Sends `SIGKILL` to that process while the node is genuinely still sleeping (i.e., after enough time has passed that the node has started, but well before `sleep_s` elapses).
3. Reads `run.json` from disk in the *parent* test process after the kill and asserts the node's status is `"running"` (proving the before-dispatch checkpoint landed).
4. Calls `resume()` (in the test process, fresh Python import, not the killed subprocess) on that run and asserts:
   - The killed node's status becomes `"failed"` with `error.code == "INTERRUPTED"` (per the existing F6 policy in `resume()`).
   - The worker's `side_effect_calls` count for that node is exactly the count from **before** the kill (should be 0 or 1 depending on whether the fake worker registers the call at entry or exit — pick whichever matches real semantics: a live LLM call that gets killed mid-flight should count as "attempted but unknown outcome," which is exactly the `INTERRUPTED` semantics — do not silently re-run it).
5. If you explicitly pass `--retry-failed` (or the equivalent `resume(..., retry_failed=True)`), THEN it's acceptable/expected for the node to re-run — that's the documented explicit-retry escape hatch. The bug is specifically about the **default** resume path silently re-executing a side-effecting node with no explicit retry instruction.

Use `subprocess.Popen` + `os.kill(pid, signal.SIGKILL)` (same technique the operator used manually) rather than any in-process simulation for this specific test — this bug class is specifically about the boundary between "the driver holds this in memory" and "this is safely on disk," which a thread-based or mocked test cannot faithfully exercise.

## Non-negotiables (same as always — do not violate)

- No `run_agent.py` edits.
- No `_HERMES_CORE_TOOLS` growth, no permanent conversation tool.
- Default-off (`workflow.enabled: false`) preserved.
- Diff scoped to `workflow/`, `tests/workflow/`, docs, and (if truly unavoidable) the existing single soft-import hunk in `hermes_cli/main.py` — do not touch it unless required.
- Do not regress any of pass-1's fixes. Full `pytest tests/workflow -q` must stay green, plus the new test(s) added this pass.
- Keep using the FS-is-authoritative / atomic-write (temp+`os.replace`) pattern already established — do not invent a second persistence mechanism for this fix.

## Process

1. Fresh branch `feat/workflow-dispatch-2` off current `main` (has pass 1 merged).
2. Read this directive fully. Read `workflow/runtime/driver.py` around the line numbers cited above to confirm current state (line numbers are approximate/may have shifted slightly on `main` vs the pre-merge worktree — locate by function name if numbers are off).
3. Implement Fix 2.1 and 2.2.
4. Implement the Fix 2.3 regression test. Run it. It must FAIL against the pre-fix code path (if you want to sanity-check, you can temporarily revert 2.1/2.2 locally to confirm the new test catches the bug, then re-apply — optional but recommended for confidence; do not commit the reverted state).
5. Full `pytest tests/workflow -q` green (should be pass-1's ~59 tests + your new one(s)).
6. Update `IMPLEMENTATION_LOG.md`: add a "Remediation pass 2" section describing the bug, the fix, and the new test. Be explicit that this is a durability gap in the *write* path that pass-1's F6 test (which only exercised the *read*/resume-decision path) did not catch — this is a useful lesson for future test design, note it.
7. Commit with a clear message.
8. Open a PR from `feat/workflow-dispatch-2` to `main` on `joeysmithjrs/hermes-agent`. **DO NOT MERGE.**
9. Print: PR URL, final pytest summary (test count), and a one-paragraph plain description of the fix suitable for a non-implementer to sanity-check.

## Stop condition

Stop when the fix is implemented, the new subprocess-kill regression test passes, full suite is green, and the PR is open — or when budget is exhausted, in which case write an honest `STATUS_PASS2.md` in `docs/proposals/workflow-dispatch/` describing exactly what's done vs not.

## Model routing

Same as before: orchestrator `z-ai/glm-5.2`; primary implementer Task agent `review-terra` (`gpt-5.6-terra`); support `code-kimi` (`moonshotai/kimi-k3`) for test scaffolding if useful. If any model call 402/5xx: retry up to 3x with backoff; fall back to `z-ai/glm-5.2` for that subtask if a model stays down rather than aborting.
