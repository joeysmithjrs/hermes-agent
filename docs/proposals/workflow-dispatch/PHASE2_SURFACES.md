# Phase 2 surfaces — cron, webhook, conversation tool

**Companion to:** `2026-07-29-workflow-dispatch-phases.md` (Phase 2 §Scope "surfaces")

Phase 2 deliberately ships **recipes over new native trigger plumbing** for cron
and webhooks. Hermes already owns a scheduler with a `no_agent` script mode and a
webhook router with HMAC validation; wiring `hermes workflow run` into both costs
zero new sacred-path code and inherits their existing auth, retry, and delivery
behavior. Native `triggers:` fields stay declared in the IR (they compile and
validate) but the *dispatching* is done by the host surface, not by a second
scheduler inside `workflow/`.

---

## 1. Cron recipe (`no_agent` script → `hermes workflow run`)

`cron/scheduler.py` short-circuits `no_agent: true` jobs to a plain subprocess —
no `AIAgent`, no LLM cost for the tick itself. That is exactly the right host for
a workflow run: the *workflow's own agent nodes* spend the tokens, the tick does
not.

```bash
hermes cron create \
  --name "morning-brief" \
  --schedule "0 7 * * *" \
  --no-agent \
  --script "$HERMES_HOME/scripts/run_workflow.sh morning_brief.yaml" \
  --deliver telegram
```

The tick's stdout is delivered by the scheduler's existing `--deliver` path, so
the run envelope JSON lands in your chat without the workflow package knowing
anything about messaging. If you also configure `workflow.notify_target`, the
run's own terminal/gate notification fires independently (see §3).

An example script ships at
[`examples-phase2-cron.sh`](./examples-phase2-cron.sh) — it is intentionally
small enough to read in one screen and copy into `$HERMES_HOME/scripts/`.

**Exit codes matter here.** `hermes workflow run` returns `0` ok, `1` runtime
fail, `2` verify reject, `3` usage, `4` awaiting gate. A cron job that treats `4`
as failure will page you every time a workflow correctly parks for human review —
the example script maps `4` to a distinct "needs approval" message instead.

---

## 2. Webhook recipe (existing route + `--script`)

`hermes webhook subscribe` already supports a `--script` route and already
validates `HMAC-SHA256` signatures against the per-route secret it generates. A
workflow trigger is therefore a subscription whose script invokes the CLI:

```bash
hermes webhook subscribe \
  --name deploy-review \
  --events "deployment.created" \
  --script "$HERMES_HOME/scripts/run_workflow.sh deploy_review.yaml" \
  --deliver slack
```

The gateway must be running (`hermes gateway run`) to receive events. The HMAC
rejection path is owned and tested by the existing webhook router — Phase 2 adds
no second signature implementation, which is the point: a duplicate HMAC route
inside `workflow/` would be a new attack surface for zero new capability.

**Gate signals over webhook** use the same shape — a route whose script runs
`hermes workflow gate <run_id> <gate_id> --decide approve`. Note that this makes
the webhook secret an approval credential; if the workflow's gate sets
`dual_control: true`, keep the approval on a human-authenticated channel instead.

---

## 3. Notifications

Run-terminal and gate-park notifications are delivered through Hermes's existing
send path (`tools/send_message_tool.py`), not a new transport. Configuration:

```yaml
# config.yaml
workflow:
  enabled: true
  notify_target: "telegram:123456789"      # any send_message target ref
  notify_on: ["failed", "partial", "awaiting_gate"]
```

Per-workflow override in the YAML:

```yaml
notify:
  on: ["succeeded", "failed", "awaiting_gate"]
  target: "slack:#ops"
```

Delivery is **best effort**: if no target is configured, the gateway is not
running, or the send fails, the attempt is logged into the run's event log and
the run continues. A notification failure never fails a workflow.

---

## 4. Conversation toolset (default off)

The `workflow` toolset (`workflow_run`, `workflow_status`) is registered only
when `workflow.tool_enabled: true`, and only at **session start** — the toolset
is fixed for the session's lifetime so a mid-turn registration can never thrash
the prompt cache. It is in no default bundle; a profile must ask for it by name.

```yaml
workflow:
  enabled: true
  tool_enabled: true
```

Gate decisions are deliberately **not** exposed as a tool. An agent that can
approve its own gates is not a gate.
