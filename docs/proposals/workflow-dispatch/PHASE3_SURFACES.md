# Phase 3 surfaces — watch, cost, chain, schedule, kanban

**Companion to:** `2026-07-29-workflow-dispatch-phases.md` (Phase 3) and
`PHASE2_SURFACES.md` (cron/webhook/conversation-tool recipes, still current).

Phase 2 answered "how does a workflow get *started* by something other than a
human at a prompt". Phase 3 answers "how does an operator *watch* one, *pay*
for one, and *sequence* one" — and it keeps the same rule: reuse the host
surfaces, don't grow a second scheduler, a second messaging transport, or a
second job store inside `workflow/`.

---

## 1. `hermes workflow watch RUN_ID`

Polls the run record until the run reaches a terminal state, then exits with a
status-derived code. It executes nothing — it is a reader.

```bash
hermes workflow watch wf_1a2b3c4d5e6f --interval 2 --timeout 600
```

| Flag | Meaning |
|---|---|
| `--interval S` | Poll period, default 2.0s |
| `--timeout S` | Give up after S seconds (default 0 = wait forever) |
| `--json` | Emit the final envelope as JSON instead of the human lines |

**Exit codes** follow the existing `hermes workflow run` convention, so a shell
script can treat both the same way:

| Code | When |
|---|---|
| 0 | run ended `succeeded` |
| 4 | run ended `awaiting_gate` — needs a human, *not* a failure |
| 1 | `failed` / `partial` / `cancelled` / `paused`, or timeout |
| 130 | Ctrl-C (prints the last known status, no traceback) |

Human mode prints a line only when something actually changes (status, or the
succeeded/failed/skipped counts), so watching a long run does not fill the
scrollback with identical lines.

`hermes workflow status RUN_ID --watch` delegates to the same loop. That flag
existed in Phase 2's help text and silently did nothing, which is worse than
not offering it.

---

## 2. Cost and tokens

```bash
hermes workflow list --cost
```

Adds a per-run token breakdown plus a `TOTAL` line. Without `--cost` the output
is byte-identical to Phase 2 (scripts parse it).

**Unknown is rendered as `-`, never as `0`.** The sqlite index carries cost but
not tokens, and pre-Phase-3 run records have no token fields at all; printing
`0` for "we don't know" is how a cost report quietly becomes a lie.

### Where the numbers come from — and the Phase 2 under-report

Phase 2 read `result["cost_usd"]` off the child result and recorded `$0.00`
for essentially every live run. The cause was not a parsing bug:
`tools/delegate_tool.py` builds the child entry with `tokens: {input, output}`
and stashes the dollar figure under `_child_cost_usd`, which
`_finalize_child_results` **pops** off the entry and folds into the *parent's*
session total. By the time `run_child_agent` returns, the per-child cost is
gone from the value the caller can see.

Phase 3 reads the counters off the child agent object, which the LiveWorker
built and still holds. That is exact and per-child — deliberately not a
before/after delta on the parent's running total, because under
`max_parallel_nodes > 1` several children fold into the same parent
concurrently and a delta would mis-attribute cost between nodes.

### Budget pause

When a run trips `max_budget_usd` it stops with status `paused` and
`pause_reason: BUDGET`, and the notification (if configured) states the cost,
the cap, and the exact resume command. Resuming **only makes progress with a
higher cap** — resuming with the same value re-checks the budget before
anything executes and immediately re-pauses. It does not silently ignore a
still-tripped cap:

```bash
hermes workflow run --resume wf_1a2b3c4d5e6f --max-budget-usd 25.00
```

**The cap is a circuit breaker, not a hard ceiling — know the overshoot.**
Cost is only known after a node-run finishes, so the breaker can only trip
*after* the work that blew it has been paid for. The bound is one dispatch
wave: at most `max_parallel_nodes` node-runs (or fanout/map branches) can be
in flight when the cap is crossed, and everything not yet started is skipped
with `skipped_reason: budget_exhausted`. So budget with `max_parallel_nodes`
in mind — a wide pool means a proportionally wider overshoot.

This is checked **inside** fanout/map branch execution, not only around it. An
earlier Phase 3 build checked the budget only in the top-level loop, which
meant a single `map` node over a 200-item list ran all 200 branches before the
cap was ever re-examined — an unbounded overshoot of the exact number the
operator set to bound spend.

---

## 3. Trigger chaining — `chain` / `run --from-run`

Run B takes its input from finished run A's final envelope.

```bash
# explicit form
hermes workflow chain wf_aaaa report.yaml --select succeeded --as upstream_nodes

# inline form — same code path (workflow/chain.py), no duplicated logic
hermes workflow run report.yaml --from-run wf_aaaa --select cost_usd --as spent
```

| Flag | Meaning |
|---|---|
| `--select PATH` | Dotted path into the source envelope (supports list indices). Omit for the whole envelope. |
| `--as KEY` | Input key to bind it to (default `from_run`) |
| `--input JSON` | Extra input keys, merged last — **explicit `--input` wins on collision** |
| `--allow-incomplete` | Chain off a non-terminal run anyway |

The derived input always also carries `source_run_id`, `source_workflow_id`,
and `source_status` so a chained run can trace its own provenance.

A path that does not resolve passes `null` rather than raising — a chain should
degrade to an absent value, not crash the target run before it starts.

**Chaining refuses a non-terminal source** unless `--allow-incomplete`. Note
this set is deliberately stricter than `watch`'s notion of terminal: `paused`
and `awaiting_gate` are checkpointed-but-resumable, so a later resume can still
add to `succeeded`/`failed`/`cost`. Chaining off one freezes a snapshot the
source run itself does not consider final.

The `workflow_chain` conversation tool exposes the same thing, minus
`--allow-incomplete`: overriding the guard is an operator decision made with
the run in front of them, not something an agent should do from chat.

---

## 4. Schedule sugar — `hermes workflow schedule`

A **thin wrapper that emits a cron recipe**. It is not a scheduler: no polling
loop, no state of its own. Hermes's cron subsystem remains the only scheduler.

```bash
# print the recipe (default) — zero side effects
hermes workflow schedule morning_brief.yaml --cron "0 7 * * *"

# actually register it with the existing cron store
hermes workflow schedule morning_brief.yaml --cron "0 7 * * *" --name morning-brief --register
```

`--register` writes a small wrapper script to `$HERMES_HOME/scripts/<name>.sh`
and shells out to `hermes cron create ... --script <name>.sh --no-agent`. The
`--no-agent` path runs the tick as a plain subprocess — no `AIAgent`, no LLM
cost for the tick itself; the workflow's own agent nodes are what spend tokens.

With `--cron` omitted, the schedule is taken from the workflow's own
`triggers: [{kind: cron, schedule: ...}]`; if neither is present it errors
rather than guessing.

The workflow is compiled and verified **before** anything is printed or
registered (exit 2 on reject). Scheduling a workflow that cannot compile is a
guaranteed 3am failure.

---

## 5. Webhook hardening

The Phase 2 webhook recipe (`PHASE2_SURFACES.md` §2) is unchanged and still
correct: a `hermes webhook subscribe --script` route whose script runs
`hermes workflow run`. Phase 3 adds no second HMAC implementation — that would
be a new attack surface for zero new capability.

Operational notes worth writing down:

- **Exit code 4 is not a failure.** A webhook-triggered workflow that parks at
  a gate returns 4. A route that treats non-zero as failure will retry the
  delivery and start the workflow *again*, so map 4 to a distinct
  "needs approval" path exactly as the cron example script does.
- **Webhook-triggered runs should carry a budget.** An externally-triggered run
  is the one most likely to be fired repeatedly by a misbehaving sender; set
  `max_budget_usd` in the workflow so a delivery storm pauses instead of
  spending without bound.
- **A gate-decision route makes the webhook secret an approval credential.** If
  the gate sets `dual_control: true`, keep approval on a human-authenticated
  channel — a shared secret is not two people.

---

## 6. Kanban projection — config-gated, and a documented stub

```yaml
workflow:
  kanban_projection: false     # default
  kanban_board: null
```

`workflow/runtime/kanban.py` ships the interface (`project_run`,
`projection_enabled`, and a `set_projector()` injection seam mirroring
`notify.set_notifier()`), and with the flag off it touches nothing.

**It is honestly a stub, and here is exactly why.** Hermes kanban tasks are
*dispatchable work items*, not passive cards: `kanban_create` requires an
`assignee` and defaults to `initial_status="running"`, so creating a card to
mirror a run would immediately queue a real dispatcher-spawned agent — which is
precisely the "kanban as execution bus" outcome the design forbids, plus real
spend on every node transition. Pinning `initial_status="blocked"` dodges the
spawn but then forecloses `kanban_complete`/`kanban_block`, both of which
validate that the task has a live dispatcher run beneath it.

A faithful one-card-per-run mirror needs a *passive card kind* in
`hermes_cli/kanban_db.py` — sacred core, out of scope here. So `project_run()`
returns `{"projected": False, "reason": "kanban_tasks_are_dispatchable_work_items"}`
rather than faking success. The residual is recorded in `IMPLEMENTATION_LOG.md`.
