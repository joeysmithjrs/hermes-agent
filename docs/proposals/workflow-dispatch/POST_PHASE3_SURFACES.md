# Post-Phase-3 surfaces — workspace, catalog, prompt library, debate, supervisor, loop-back

**Companion to:** `POST_PHASE3_SPEC.md` (the architecture) and
`PHASE3_SURFACES.md` (watch/cost/chain/schedule, still current).

Phase 3 answered "how does an operator watch, pay for, and sequence a run".
These extensions answer "how do runs *accumulate*" — shared state that survives
a run, recipes that survive an edit, prompts that survive a copy-paste, and two
multi-agent shapes (debate, supervisor) that were previously only expressible by
hand-wiring a graph and hoping it terminated.

Same rule as every phase before it: reuse the host surfaces. No new scheduler,
no new transport, no new job store, no edits to `run_agent.py`,
`model_tools.py` or `tools/delegate_tool.py`.

Runnable examples live next to this file:

| File | Shows |
|---|---|
| `examples-post-phase3-workspace.yaml` | `workspace:` + `spec.prompt: {library: ...}` |
| `examples-post-phase3-debate.yaml` | `debate` with `judge_escalate` |
| `examples-post-phase3-supervisor.yaml` | `supervisor` with `ask_on_uncertain` |
| `examples-post-phase3-catalog.sh` | `register` / `list-catalog` / `run-catalog` / `restart` |

---

## 1. Named persistent workspaces

```yaml
workflow: desk_autonomy
workspace: desk-autonomy      # workflow-level, not per-node
```

```
$HERMES_HOME/workflows/workspaces/desk-autonomy/
    seed.md                 <- persists across runs
    protocol_state.yaml     <- persists across runs
    runs/<run_id>/          <- this run's private corner
```

A workspace is ACTIVE (agents read *and* write it). It is deliberately not the
run artifact store: `workflows/runs/<run_id>/` is the AUDIT record — written by
the driver, read-only to agents, never mutated after the fact. Cross-run seeding
is the feature: a run leaves a file behind instead of threading prompt text
through the CLI.

Template ctx (paths and names only — **never file bodies**):

| Reference | Value |
|---|---|
| `{{ workspace.name }}` | the declared name |
| `{{ workspace.dir }}` | absolute workspace path |
| `{{ workspace.run_dir }}` | this run's subdirectory |
| `{{ workspace.run_id }}` | this run's id |
| `{{ workspace.files }}` | top-level entry names (`runs/` excluded) |
| `{{ workspace.previous_runs }}` | earlier run ids, oldest first |

Why paths and not contents: the child's context payload is built at run time
from these strings, so a workspace file changing between runs cannot perturb a
cached prompt prefix. **Prompt caching stays byte-stable.**

`{{ workspace.* }}` in a workflow that declares no `workspace:` is a compile
error (`TEMPLATE`) — the ctx root would not exist at run time. Names are
validated (`[a-z0-9][a-z0-9_-]*`, ≤64) and every path is containment-checked, so
a name or a relative path cannot escape the workspaces root. Workspaces live
under `HERMES_HOME`, so a different profile sees a different set — the same
per-profile boundary `cron/jobs.py` relies on.

---

## 2. Versioned workflow catalog

```bash
hermes workflow register --id desk-council --from-file council.yaml \
  --tags desk,debate --owner joe --description "..."
hermes workflow list-catalog --tags desk [--all-versions] [--json]
hermes workflow run-catalog desk-council [--version 2] [--params '{"market":"energy"}']
```

Stored as `workflows/catalog/index.yaml` + `workflows/catalog/<id>/version_<v>.yaml`.
Re-registering an id writes the next version; earlier versions stay on disk and
stay runnable. Registration never touches run artifacts.

`register` snapshots the YAML and deliberately does **not** compile it: a
parameterized recipe carries `{{ params.* }}` placeholders that only resolve at
`run-catalog` time, so demanding a clean compile at registration would make every
parameterized recipe unregisterable. `run-catalog` compiles **and verifies**
before anything executes.

---

## 3. Prompt library (`spec.prompt: {library: ...}`)

```yaml
spec:
  prompt:
    library: debate-participant-v1
    params:
      topic: "{{ seed.output.echo.topic }}"
      stance: "argue for cutting the exposure"
```

Named, reusable prompt bodies at `$HERMES_HOME/workflows/prompts/<name>.yaml`
(operator-owned, shadows) then `workflow/prompts/builtin/<name>.yaml` (shipped).

**Why `library:` and not `template:`** — "template" already means `expr.py`'s
`{{ node_id.output.field }}` interpolation in this codebase, checked by the
verifier under the error code `TEMPLATE`. A second unrelated "template" concept
would leave an author unable to answer "does my `{{ }}` resolve against the
registry or against node outputs?".

Two-stage rendering: `{{ params.* }}` is substituted at load (missing param →
fail closed), then the result goes through the ordinary `{{ node.output.* }}`
render against live run data. Pure file read + string format — no model call.
Unknown library is a hard compile error (`PROMPT_LIBRARY`); there is no
"not found → empty prompt" path.

Shipped builtins: `debate-participant-v1`, `workspace-review-v1`.

---

## 4. `debate` node

```yaml
- id: desk_debate
  kind: debate
  protocol: judge_escalate     # vote | judge_escalate | continue
  max_rounds: 3                # REQUIRED -- a debate always terminates
  directive:
    topic: "..."               # REQUIRED
    objective: "..."
    vote_key: verdict          # vote on this field, not the whole message
    threshold: 2               # default: strict majority of votes cast
    judge_model: "..."         # judge_profile is REJECTED (F2)
    judge: {id: chair, kind: agent, spec: {prompt: "..."}}
  participants:                # int N (clones `branch:`) or a list of templates
    - {id: bull, kind: agent, spec: {prompt: "..."}}
    - {id: bear, kind: agent, spec: {prompt: "..."}}
```

ONE node-run that internally runs `participants x max_rounds` agent turns
through the ordinary worker. Rounds live inside the node — **not** as edges — so
the graph stays acyclic and the ceiling is arithmetic on two numbers visible in
the YAML.

| Protocol | Stops when | Output `result` |
|---|---|---|
| `vote` | a round reaches `threshold` (else a strict majority of votes cast) | `majority` reducer |
| `judge_escalate` | same; still split at `max_rounds` → the judge rules | `judge_converge` (ruling **and** the tally it ruled against) |
| `continue` | `max_rounds`, always | `concat` |

`vote` that never converges **fails** the node (`DEBATE_DIVERGED`): a vote exists
to produce a decision, and reporting `succeeded` with no verdict hands downstream
a decision nobody reached. Use `continue` (or `on_fail: continue`) when
divergence is an acceptable outcome.

Turn context (`{{ branch.* }}`): `role`, `round`, `max_rounds`, `topic`,
`objective`, `transcript`. Every participant in a round sees the **same**
transcript (previous rounds only), so a round is order-independent — feeding a
participant its same-round predecessors would silently privilege whoever spoke
last.

`reduce:` (optional) replaces the protocol's default reducer over the same
envelope set.

Audit: `wf_<id>__debate_<node>__round_2__agent_bear__<uuid>` — one node_run_id
per turn, so `workflow logs` reconstructs who argued what, when.

Bounds and failures: the run budget breaker is armed **inside** the round loop
(a debate is one node-run that bills N times); a failed participant is tolerated
(the debate converges on the surviving arguments) and stays `failed` on the
checkpoint without wedging downstream nodes; a debate where *no* turn produced an
argument fails (`DEBATE`).

**Compile-time rejects:** missing `directive.topic`, missing/`<1` `max_rounds`,
unknown protocol, fewer than 2 participants, `judge_escalate` without
`directive.judge`, `judge_profile`, a participant that is itself a
`debate`/`supervisor` (unbounded recursive spend), and any participant-template
error the full node rule set catches (missing prompt, unknown tool,
unregistered `run:`). F3/F6 read the CHILD templates, so a live tool cannot
launder itself past gating by nesting.

---

## 5. `supervisor` node

```yaml
- id: desk_lead
  kind: supervisor
  advisory_policy: ask_on_uncertain   # REQUIRED
  budget: 2                           # REQUIRED (hard cap on advisor calls)
  max_advisory_rounds: 2              # may bind tighter than budget
  supervisor_model: "...haiku..."
  advisor_model: "...opus..."
  advisory_context: "Desk policy v3: ..."
  spec: {prompt: "..."}               # the SUPERVISOR agent itself
  advisor: {id: specialist, kind: agent, spec: {prompt: "..."}}
```

The node's own `spec:` is the supervisor agent. The node's output is always the
**supervisor's final turn** — never the advisor's. Advice reaches the supervisor
as `{{ branch.advice }}` on its next turn.

| Policy | Consults when |
|---|---|
| `ask_on_uncertain` | the supervisor's own output contains `request_advisory: true` |
| `always_ask` | every round, up to the caps |
| `budget` | deliberately spends the budget, then answers |
| `never_ask` | never — no advisor is constructed at all |

`ask_on_uncertain` acts on that **one documented signal**. No invented
confidence threshold: a number the driver made up is one no author wrote and no
reader could predict.

`budget` is a hard STOP, not a silent skip — `stopped_reason` records which cap
ended the loop (`budget`, `max_advisory_rounds`, `no_request`, `advisor_failed`,
`budget_exhausted`, `never_ask`). An advisory round is atomic (advisor turn, then
the supervisor turn that reads the advice), so the run-budget breaker bounds
overshoot to one round rather than paying for advice nobody read.

A failed advisor does not fail the supervision — the supervisor keeps the answer
it already had, and the failed turn stays `failed` on the checkpoint. A failed
**first** supervisor turn fails the node (`SUPERVISOR`): there is no answer to
advise on.

`supervisor_model` / `advisor_model` / `advisor_provider` apply only where the
child template declares nothing (the more specific declaration wins), through the
same `spec.model`/`spec.provider` path Phase 2 already honors.

Audit: `wf_<id>__supervisor_<node>__turn_2__agent_supervisor__<uuid>` and
`...__adv_1__agent_advisor__<uuid>`. Cost = supervisor turns + advisor turns,
each counted once; the node's own `cost_usd` is a display rollup.

**Compile-time rejects:** missing/unknown `advisory_policy`, missing `budget` for
any asking policy, missing `advisor:` template, an advisor that is itself a
`supervisor`/`debate`, and (as for any agent node) a missing prompt, an
override-only field, or an unknown tool. `never_ask` plus an `advisor:` warns —
it will never run.

---

## 6. Loop-back — `restart`, lineage, and why it is not a cycle

The graph stays **acyclic** (the verifier still rejects cycles). A backward route
is a NEW run linked by recorded lineage: previous artifacts stay intact,
checkpoint durability is untouched, and the history stays reconstructable —
which is precisely what a real cycle would have blurred.

```bash
# re-run the SAME workflow a prior run used, seeded from its own output
hermes workflow restart wf_1a2b3c4d5e6f --select succeeded --as previous_nodes

# seed from a DIFFERENT run than the one being restarted
hermes workflow restart wf_1a2b --input-from-run wf_9z8y

# name the target workflow explicitly (loop into a different workflow)
hermes workflow chain wf_1a2b other.yaml --select status --as prev
hermes workflow run other.yaml --from-run wf_1a2b        # inline form
```

`restart` needs no path: the definition is the one the source run compiled, and
it is **re-verified at load** (same posture as `resume`), so a tampered
`definitions/<id>.json` is rejected rather than trusted because an earlier run
used it.

Every seeded run records its lineage on the checkpoint, the run envelope, and
`hermes workflow status`:

```json
"from_run": {"run_id": "wf_1a2b3c4d5e6f", "workflow_id": "desk_council",
             "status": "succeeded", "select": "succeeded", "as": "previous_nodes"}
```

The `select`/`as` pair is part of the record on purpose: lineage that says a run
was seeded from another without saying *with what* is the half of the provenance
you do not need when the loop misbehaves.

To continue the **same** run instead of starting a new one, resume:

```bash
hermes workflow run --resume wf_1a2b --from-node desk_debate --retry-failed
```

(`--from-node` is a spelled-out alias of the existing `--from`.)

---

## Non-goals (unchanged)

- **No true cyclic graph.** Loop-back is trigger-chaining, not a cycle.
- **No FakeWorker default.** `HERMES_WORKFLOW_FAKE=1` or an explicit `--fake`;
  otherwise a workerless driver refuses rather than reporting canned successes.
- **No `run_agent.py` / `model_tools.py` / `delegate_tool.py` edits.** Debate and
  supervisor children go through the existing `LiveWorker` → `build_child_agent`
  path.
- **No mid-run system-prompt mutation.** Round/advice context travels in the
  per-turn context payload, never by rewriting a cached prefix.
