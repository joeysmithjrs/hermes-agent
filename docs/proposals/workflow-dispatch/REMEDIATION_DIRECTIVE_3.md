# DIRECTIVE — Remediation pass 3: conditional edges are parsed but never evaluated

**Implementer/orchestrator for this pass:** you (Claude Code, running on Joe's Claude Pro
subscription — single model, no OpenRouter/CCR routing this run). No multi-agent Task
fan-out needed for this one; the bug is small and localized. Work it yourself directly.

**Budget:** subscription-billed (no per-token dollar cap this run). Still be efficient —
don't burn huge context on exploration you don't need. Aim to land this in well under an
hour of wall-clock work.

**Branch:** `feat/workflow-dispatch-3` — fresh off current `main` (passes 1 and 2 already
merged: node_run_id keying, frozen script registry, nested-override rejection, gate-resume
safety, and the checkpoint-before-blocking-call durability fix are all already in and
independently live-verified by the operator — do not re-litigate any of those).

## The bug — confirmed live by the operator, not just read from code

**Location:** `workflow/expr.py` has a fully-implemented condition language:
`eval_condition(condition: str, upstream_output: Any) -> bool`, supporting
`$.field op literal` with `==, !=, >, >=, <, <=, in, exists`, plus bare `true`/`false`.
It is exported (`__all__`) and the `Edge` dataclass in `workflow/ir.py` has a `condition:
Optional[str]` field that parses from YAML and round-trips to JSON correctly.

**But it is never called anywhere in the driver.** Confirmed by grep: the only references
to `eval_condition` are its own definition and its appearance in `__all__`. The driver's
readiness logic — `Driver._upstreams_done()` in `workflow/runtime/driver.py` — only checks
`node_runs[...].status == "succeeded"` for each upstream. It has zero awareness of
`Edge.condition`.

**Operator's live repro (already run, confirmed the bug, do not need to re-run to believe
it — but DO write an equivalent regression test):**

```yaml
workflow: test_conditional
version: 1
nodes:
  - id: check
    kind: script
    run: workflow.examples.echo
    input: { score: 3 }
  - id: stop_path
    kind: script
    run: workflow.examples.echo
    input: { branch: "stop" }
  - id: continue_path
    kind: script
    run: workflow.examples.echo
    input: { branch: "continue" }
edges:
  - { from: check, to: stop_path, condition: "$.score < 5" }
  - { from: check, to: continue_path, condition: "$.score >= 5" }
triggers: [{ kind: manual }]
```

With `score: 3`, only `$.score < 5` is true. Expected: only `stop_path` runs.
**Actual (bug):** BOTH `stop_path` AND `continue_path` ran and succeeded. The `condition`
field is silently ignored — this is worse than a hard failure because it looks like
correct behavior in a status/log output that doesn't scrutinize which nodes ran.

This is exactly the "script returns X → stop, LLM returns Y → continue" pattern the
operator asked about, and it is load-bearing for future work (e.g. a kill/no-trade
classifier gate in a PM-style pipeline) — hence the priority.

## Required fix

### Fix 3.1 — wire `eval_condition` into readiness

In `Driver._upstreams_done()` (or the most architecturally correct spot — you may need to
adjust which function decides per-edge satisfaction vs per-node upstream-set satisfaction;
read the current code fully before deciding), for each incoming edge to a node:

1. If `edge.condition` is `None` — current behavior (upstream succeeded is sufficient),
   unchanged.
2. If `edge.condition` is set — the node is only "ready via this edge" if the upstream
   node's output satisfies `eval_condition(edge.condition, upstream_output)`. If it does
   not, that edge does NOT count toward satisfying the downstream node's readiness.

**Semantics to get right (read design/api docs under `docs/proposals/workflow-dispatch/`
for any additional context on intended condition semantics before assuming — check
`2026-07-29-workflow-dispatch-design.md` and `-api.md` for any existing `condition`
discussion; align with documented intent where it exists, otherwise use the reasoning
below):**

- **A node with a single incoming conditional edge:** if the condition is false, the node
  should NOT run at all — it should end up in a `skipped` state (there is already a
  `skipped` list in the RunEnvelope/`_run_envelope()` output — confirm whether it's
  currently ever populated; if not, this is the first real producer of it). Do NOT mark it
  `failed` — a false condition is an intentional non-execution, not an error.
- **A node with multiple incoming edges, some conditional some not (fan-in join
  semantics):** the existing `from: [...]` list-based upstream requirement (used by `join`
  nodes) is a DIFFERENT mechanism from `edges: [...]` condition-bearing edges — check
  whether these two ever coexist on the same node in the current IR/verifier and handle
  accordingly; if they cannot coexist per the verifier, document that constraint rather
  than silently assuming.
- **Multiple parallel conditional edges off the same upstream node (the test_conditional
  case above):** each downstream node evaluates its own inbound edge condition
  independently against the shared upstream's output. It is valid and expected for zero,
  one, or multiple downstream branches to be satisfied depending on the literal condition
  values — the bug is specifically that BOTH ran when the literals were mutually exclusive
  (`< 5` and `>= 5` can never both be true), not that multiple conditions can never both be
  true in principle (a badly-written pair of conditions COULD both be true — that's a
  workflow-authoring correctness concern, not a driver bug — the driver's job is only to
  evaluate each condition correctly and independently).

### Fix 3.2 — skipped nodes must not deadlock downstream nodes that require them

If a node is skipped (condition false, no other satisfying edge), any node downstream of
ONLY that skipped node must also become skipped (or otherwise correctly resolve — do not
leave the run stuck in "not ready, not skipped, not failed, not succeeded" limbo forever).
Propagate skip status transitively where correct. Write a test with a 3-node chain
(A —cond false--> B —normal edge--> C) confirming B AND C both end up `skipped`, and the
overall run finalizes (not stuck as `running`/`partial` forever).

### Fix 3.3 — verifier should sanity-check condition syntax at compile time, not just at runtime

Currently `eval_condition` raises `TemplateError` for unparseable conditions ("fail
closed"). Check whether `verify.py` currently validates `edge.condition` strings at
compile time (i.e. does `hermes workflow validate` catch a malformed condition BEFORE a
run, or does it only blow up mid-run?). If validate does not currently catch this, add a
verifier check that parses/validates the condition syntax (reuse `eval_condition`'s regex
or a shared syntax-check helper — do not duplicate the regex in two places) so `validate`
rejects malformed conditions before any run starts. This is a nice-to-have hardening step,
not the core bug — do it if time/budget allows after 3.1 and 3.2 are solid and tested;
otherwise document it as a known residual gap.

## Regression tests (required)

1. **Reproduce the exact bug** as a test: the `test_conditional` YAML above (or equivalent
   inline fixture), run via `FakeWorker`, assert `stop_path` is in `succeeded` and
   `continue_path` is in `skipped` (NOT in `succeeded`, NOT in `failed`).
2. **Skip propagation chain test** per Fix 3.2 above.
3. **Existing non-conditional edges still work unchanged** — add or confirm an existing
   test still passes proving a plain unconditional edge (`condition: None`) behaves exactly
   as before this fix (no regression on the common case).
4. If you implement 3.3, add a test that `hermes workflow validate` rejects a workflow with
   a malformed `condition:` string (e.g. `"$.score <> 5"` or arbitrary non-DSL text) with a
   clear compile-time error, not a runtime crash.

Run the FULL existing suite (`pytest tests/workflow -q`) and confirm it stays green — should
be 60 passed (from pass 2) plus your new tests.

## Non-negotiables (same as always)

- No `run_agent.py` edits.
- No `_HERMES_CORE_TOOLS` growth, no permanent conversation tool.
- Default-off (`workflow.enabled: false`) preserved.
- Diff scoped to `workflow/`, `tests/workflow/`, docs — do not touch
  `hermes_cli/main.py` unless truly unavoidable (it should not be needed for this fix).
- Do not regress any of pass 1 or pass 2's fixes. Do not "improve" unrelated code you
  happen to read while investigating this bug — stay scoped.
- Keep using the existing atomic-write / FS-is-authoritative pattern; do not invent new
  persistence mechanisms.

## Process

1. Read this directive fully.
2. Read `workflow/runtime/driver.py` (`_upstreams_done`, `_compute_ready`, `_run_envelope`,
   the `skipped` list usage), `workflow/expr.py` (`eval_condition`), `workflow/ir.py`
   (`Edge`, `Node.from_`), and `workflow/verify.py` (to check current condition-syntax
   validation state) BEFORE writing any code, to confirm current line numbers/structure
   (this directive's descriptions are accurate as of the operator's live testing but may
   not have exact line numbers — locate by function/class name).
3. Implement Fix 3.1 and 3.2. Get regression tests 1-3 passing.
4. If budget/time allows, implement Fix 3.3 and test 4. If not, document it as a residual
   gap in `IMPLEMENTATION_LOG.md` under a new "Remediation pass 3" section — do not silently
   skip it without a note.
5. Full `pytest tests/workflow -q` green.
6. Update `IMPLEMENTATION_LOG.md` with a "Remediation pass 3" section: describe the bug,
   the fix, the skip-propagation semantics chosen, and what (if anything) was deferred.
7. Commit with a clear message.
8. Open a PR from `feat/workflow-dispatch-3` to `main` on `joeysmithjrs/hermes-agent`.
   **DO NOT MERGE.**
9. Print: PR URL, final pytest summary (test count), and a short plain-English description
   of the fix and the skip-propagation semantics you chose, suitable for the operator to
   sanity-check before merging.

## Stop condition

Stop when Fix 3.1 + 3.2 are implemented and tested (green), the PR is open, and
`IMPLEMENTATION_LOG.md` is updated — Fix 3.3 is optional/best-effort. If you get stuck or
something in the existing code contradicts this directive's assumptions, stop and write an
honest note in `IMPLEMENTATION_LOG.md` (or a new `STATUS_PASS3.md`) explaining the
discrepancy rather than guessing or working around it silently.
