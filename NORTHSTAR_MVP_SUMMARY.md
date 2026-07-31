# PM Desk North Star MVP — what shipped, and what Joe does next

Branch `feat/pm-desk-northstar-mvp`, on top of PR #9 (`4f58957ec`).
Everything lives under `optional-projects/pm-desk/`. Nothing was added to the
root `package.json`, the npm workspaces, the model-tool schema, or the default
Hermes install.

---

## The exact file for the first live run

```
optional-projects/pm-desk/workflows/pm-morning-generator-v0.yaml
```

That is the workflow. `pm-desk-paper-v0.yaml` is superseded and
`pm-signal-adjudication-v0.yaml` is an optional subroutine for a fired signal,
not the morning loop.

---

## What PR #9 was missing, and what this adds

PR #9 built the substrate: Polymarket public client, Browserbase collector,
SQLite + CAS store, monitor engine, ingress, paper ledger, taxonomy seed. What
it did not have was a way for a morning's research to *become* anything. The
spine ended at a bare gate. An approval reached nothing.

This PR builds the control plane between those two halves:

| | |
|---|---|
| **ExecutionPlan** (`src/schema/execution-plan.ts`) | The artifact the generator emits and the gate approves. Edge story, monitors, and the exact setup commands. `paper_only: true` and `live_execution_allowed: false` are zod **literals**, so a live-execution plan cannot be represented in the type. |
| **Plan CLI** (`src/cli/commands/plan.ts`) | validate / show / render-telegram / schema / from-run / approve. |
| **Provisioner** (`src/provision/`) | dry-run / apply / status / revoke. Pure derivation, then execution. No agent. |
| **Generator workflow** | `pm-morning-generator-v0.yaml` — prepare → seed → directive → dq → **dd (real research tools)** → eval → **plan** → paper_gate. |
| **Prompt libraries** | `pm-dd-v1` rewritten for tool-using research; `pm-eval-v1` binds a survivor to concrete monitors; **`pm-execution-plan-v1`** is new. |

The four divergences named in the directive are corrected:

1. **DD had `tools: [read_file]` and no browse.** It now has `web_search`,
   `web_extract`, `read_file` and browser navigation/snapshot/click/scroll — and
   a test asserts research tools are granted to *exactly one node in one
   workflow*, so the correction cannot spread.
2. **Signal adjudication was the docs' centre of gravity.** The README now opens
   with the generate→approve→provision→monitor loop; adjudication is documented
   as an optional subroutine a provisioned monitor may hand a fired signal to.
3. **The spine ended at a bare `paper_gate`.** It now ends at an ExecutionPlan,
   and approval leads to a deterministic install.
4. **Agent-free detection was presented as the product.** It is the monitor
   engine *inside* provisioned monitors. The product is the morning loop.

---

## Three properties worth checking in review

**No string an agent wrote is ever executed.** The plan's `apply_command` is
what Joe *read*. The argv the provisioner spawns is derived from the plan's
validated monitors and passed to `execFile` as an array. When the two disagree
that is reported as drift and the derived one still runs.

**Nothing but Hermes can approve a plan.** `pm-desk plan approve` reads the
decision Hermes wrote to `runs/<id>/gate_signals/<gate>.json` when Joe answered
Telegram, and never writes it. `modify` maps to **denied** — "come back with
changes" is a refusal of *this* plan. `provision apply` additionally requires
`--i-approved-this-plan`.

**The provisioner cannot be a workflow node, and the code says so rather than
pretending.** Hermes' script registry is frozen at import and holds exactly
`best`, `concat`, `first_k`, `judge_converge`, `majority`, `top_k`,
`workflow.examples.echo`, `workflow.examples.notify_telegram`. There is no
`pm_desk.*` callable and this package does not add one. The bridge is the CLI,
the same shape the paper ledger already uses.

---

## Two things found in Hermes while building this

Both are verified against `workflow/expr.py` on this host, and both shaped the
generator's graph.

1. **`condition:` on an edge out of an agent node can never fire.** An agent's
   stored output is `{text, node, result, ...}` with the model's JSON as a
   *string* inside `text`; it is never parsed. So `$.decision == 'advance'`
   raises `TemplateError`, which `_edge_state` fails closed to `"blocked"` —
   silently skipping everything downstream.
2. **`over:` against an agent output fails the node outright.**
   `{{ dq.output.survivors }}` raises `TemplateError` from `render`.

`pm-desk-paper-v0.yaml` has both shapes, so it could never have reached DD in a
live run. Its header now says so. The generator uses `over:` only against the
seed's directive cards (real input data) and has no conditions at all; a test
asserts that, so the bug cannot come back.

---

## Verification — real output from this host

```console
$ npm run check
Test Files  15 passed (15)
     Tests  294 passed | 6 skipped (300)

> pm-desk@0.1.0 build
> tsc -p tsconfig.build.json
```

```console
$ PM_DESK_HERMES_INTEGRATION=1 npx vitest run tests/hermes.test.ts
 ✓ tests/hermes.test.ts (25 tests) 6904ms
   ✓ real Hermes CLI (opt-in) > validates the adjudication workflow with no prompt libraries installed
   ✓ real Hermes CLI (opt-in) > rejects the research spine until its prompt libraries are installed, then accepts it
   ✓ real Hermes CLI (opt-in) > accepts the exact argv the launcher builds (compile + plan only, no agent)
   ✓ real Hermes CLI (opt-in) > rejects the morning generator until its prompt libraries are installed, then accepts it
   ✓ real Hermes CLI (opt-in) > accepts the generator's research tool grant — the names really exist on this host
   ✓ real Hermes CLI (opt-in) > plans the generator's first node from a real compiled seed, spawning no agent
```

```console
$ hermes workflow validate workflows/pm-morning-generator-v0.yaml
OK  workflow=pm_morning_generator_v0 hash=sha256:9efb9e6136c35b2b2c0fa577e9e4c8af3f2b1351cb5177f70391f0e25ea9f1a5

$ hermes workflow run workflows/pm-morning-generator-v0.yaml --input "$(cat /tmp/seed-wrapped.json)" --dry-run
{
  "run_id": "wf_7faa0d049282",
  "workflow_id": "pm_morning_generator_v0",
  "status": "dry_run",
  "ready": [
    "prepare"
  ],
  "node_runs": 1
}
```

```console
$ pm-desk provision dry-run --plan fixtures/plans/example_execution_plan.json \
    --hermes-home $(mktemp -d) --desk-home $(mktemp -d)
DRY RUN — nothing written, nothing spawned. plan plan_9f3c1d0a7b6e4c2f8a15d3e7c9b04628
PAPER ONLY — no monitor installed here can place an order.

FILES (9)     [plan.json, 1 source spec, 3 monitor specs, 2 sweep scripts, 2 copies into $HERMES_HOME/scripts]

COMMANDS (3)
  [planned] cron_create
    hermes cron create 1h --no-agent --script pm-desk-plan_9f3c...-1h.sh --name pm-desk-plan_9f3c...-1h --deliver telegram
  [planned] cron_create
    hermes cron create 30m --no-agent --script pm-desk-plan_9f3c...-30m.sh --name pm-desk-plan_9f3c...-30m --deliver telegram
  [planned] install_prompts
    pm-desk hermes install-prompts --hermes-home /tmp/tmp.16OKYRnlPC --apply

NOT EXECUTED (1) — printed for you to run or ignore
  [webhook_subscribe_recipe] # recipe only: point an external notifier at the desk ingress on 127.0.0.1:8787 yourself
    enabling a Hermes webhook edits your Hermes config, which pm-desk never does — run it yourself if you want it
```

The full approve → apply → status → revoke chain was also exercised end to end
against temp homes and a stub `hermes` binary: two cron jobs created and their
ids recorded, prompt libraries installed, the webhook item skipped, then both
jobs removed by recorded id with the artifacts deliberately retained.

**No live LLM run was made.** Every workflow here validates and dry-runs; none
has reached a model. That is the next step and it needs Joe.

---

## What Joe does after merge

```bash
export PATH="$HOME/.local/node-v24.18.1-linux-x64/bin:$PATH"
cd optional-projects/pm-desk && npm ci

export HERMES_HOME="$HOME/.hermes"      # or a throwaway: $(mktemp -d)
export PM_DESK_HOME="$PWD/data"

# 1. Prompt libraries into that home.
npx tsx src/cli/pm-desk.ts hermes install-prompts --hermes-home "$HERMES_HOME" --apply
hermes workflow validate workflows/pm-morning-generator-v0.yaml

# 2-3. Compile the morning seed and wrap it (see the README for the wrapper).
npx tsx src/cli/pm-desk.ts taxonomy compile --max-cards 3 --json > /tmp/seed.json

# 4. Dry-run first (free), then the real run (~$2 ceiling, ends awaiting_gate).
hermes workflow run workflows/pm-morning-generator-v0.yaml \
  --input "$(cat /tmp/seed-wrapped.json)" --dry-run
hermes workflow run workflows/pm-morning-generator-v0.yaml \
  --input "$(cat /tmp/seed-wrapped.json)"

# 5. Approve or deny in Telegram.

# 6-9. The deterministic half.
npx tsx src/cli/pm-desk.ts plan from-run --run-id <id> --hermes-home "$HERMES_HOME" --out /tmp/plan.json
npx tsx src/cli/pm-desk.ts plan validate --file /tmp/plan.json
npx tsx src/cli/pm-desk.ts plan approve  --file /tmp/plan.json --run-id <id> --hermes-home "$HERMES_HOME"
npx tsx src/cli/pm-desk.ts provision dry-run --plan /tmp/plan.json --hermes-home "$HERMES_HOME" --desk-home "$PM_DESK_HOME"
npx tsx src/cli/pm-desk.ts provision apply   --plan /tmp/plan.json --hermes-home "$HERMES_HOME" --desk-home "$PM_DESK_HOME" --i-approved-this-plan
```

Full walkthrough: README → **First live morning run**.

### Expect the first plan to need a hand-fix

`spec.output` is stored by Hermes and enforced by no execution path, so the only
thing making the plan node's JSON valid is the prompt. If `plan validate`
rejects the first one, the JSON is right there in `/tmp/plan.json` — fix it,
re-validate, and carry on. A failed morning is a wasted morning, not a broken
desk.

---

## Not in this PR, deliberately

- Live trading, wallets, signing, order placement, UMA. The guard test blocks
  the symbols and the plan schema cannot express live execution.
- Auto-enabling Hermes webhooks. Recipes are printed; enabling one edits the
  operator's Hermes config, which this package does not do.
- Adding `pm_desk.*` callables to Hermes' frozen `run:` allowlist. That is a
  core edit; the CLI bridge is documented instead.
- Multipath neg-risk market making, the wallet skill-score product.
- Any spend on a live generator run.
