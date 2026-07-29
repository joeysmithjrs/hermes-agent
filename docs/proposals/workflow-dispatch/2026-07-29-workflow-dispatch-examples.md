# Workflow Dispatch — Examples

**Companion to:** design + api · 2026-07-29

Illustrative only — match IR semantics; not production-executable until runtime lands.

---

## 1. Linear: research → write → notify

### YAML

```yaml
workflow: linear_brief
version: 1
defaults:
  model: openrouter/x-ai/grok-4.5
  timeout_s: 600
nodes:
  - id: research
    kind: agent
    spec:
      prompt: |
        Research {{ input.topic }}. Return JSON { "bullets": [str], "sources": [url] }.
      tools: [web_search, web_extract]
      output:
        type: object
        required: [bullets, sources]
  - id: write
    kind: agent
    spec:
      prompt: |
        Write a tight briefing from:
        {{ research.output | tojson }}
      tools: [write_file]
      output:
        type: object
        required: [path]
  - id: notify
    kind: script
    run: workflow.examples.notify_telegram
    input:
      path: "{{ write.output.path }}"
edges:
  - { from: research, to: write }
  - { from: write, to: notify }
triggers:
  - { kind: manual }
```

### CLI

```bash
hermes workflow validate examples/linear_brief.yaml
hermes workflow run examples/linear_brief.yaml --input '{"topic":"UMA oracle race"}'
```

---

## 2. Fan-out → join → dual-control gate → exec

```yaml
workflow: fanout_gate_exec
version: 1
max_budget_usd: 3.0
nodes:
  - id: seed_urls
    kind: script
    run: demo.seed_urls          # returns { "urls": [...] }
  - id: scrape
    kind: fanout
    over: "{{ seed_urls.output.urls }}"
    branch:
      kind: agent
      spec:
        prompt: "Summarize {{ branch }} in <=120 words. JSON {summary, risk_flags}"
        tools: [web_extract]
        budget_usd: 0.15
        max_turns: 8
  - id: reduce
    kind: join
    from: [scrape]
    reduce: { type: concat }
    run: demo.concat_summaries   # -> { candidates: [...] }
  - id: rank
    kind: agent
    spec:
      prompt: "Pick top 1 candidate from {{ reduce.output }}. JSON { pick, why }"
      tools: []
  - id: freeze
    kind: gate
    # or reference gates.freeze + edge into it — both allowed by design
  - id: speak
    kind: agent
    spec:
      prompt: "Prepare outbound message for {{ rank.output.pick }} (DRAFT ONLY)"
      tools: []
edges:
  - { from: seed_urls, to: scrape }
  - { from: scrape, to: reduce }
  - { from: reduce, to: rank }
  - { from: rank, to: freeze }
  - { from: freeze, to: speak, port: approve }
gates:
  freeze:
    id: freeze
    channel: telegram
    approvers: [joe]
    dual_control: true
    on_timeout: shelve
```

### Gate from CLI / chat

```bash
hermes workflow gate wf_01H… freeze --decide approve --note "lgtm"
```

---

## 3. PM Autonomy Pipeline skeleton

Maps vault stages → IR (see design §11).

```yaml
workflow: pm_desk_v0
version: 1
max_budget_usd: 5.0
defaults:
  model: openrouter/x-ai/grok-4.5
nodes:
  - id: prepare
    kind: agent
    spec:
      prompt_file: prompts/pm/prepare.md
      tools: [read_file, session_search]
      output: { $ref: schemas/desk_state.json }
      workspace: { kind: dir, path: null }   # runtime fills runs/<id>/ws/prepare

  - id: seed
    kind: script
    run: pm_desk.seed_branches              # deterministic exploration pressure
    idempotent: true

  - id: directive
    kind: fanout
    over: "{{ seed.output.branches }}"
    branch:
      kind: agent
      spec:
        prompt_file: prompts/pm/directive.md
        tools: [web_search, web_extract, read_file]
        budget_usd: 0.25
        max_turns: 12

  - id: dq
    kind: join
    from: [directive]
    reduce: { type: top_k, k: 3 }
    run: pm_desk.dq_filter                  # hard+soft DQ; emit candidates[]

  - id: dd
    kind: fanout
    over: "{{ dq.output.candidates }}"
    branch:
      kind: agent
      spec:
        prompt_file: prompts/pm/dd.md
        tools: [web_search, web_extract, write_file]
        output: { $ref: schemas/dd_memo.json }
        budget_usd: 0.4

  - id: eval
    kind: join
    from: [dd]
    reduce: { type: scorecards }
    run: pm_desk.eval_score                 # default-reject bias

  - id: plan
    kind: agent
    spec:
      prompt_file: prompts/pm/plan.md
      tools: []                              # no trading tools yet
      output: { $ref: schemas/plan.json }

  - id: freeze
    kind: gate

  - id: exec
    kind: agent
    spec:
      profile: trader-paper
      prompt_file: prompts/pm/exec.md
      tools: [/* paper trade tools only */]
      budget_usd: 0.1
      max_turns: 15

  - id: monitor
    kind: fanout
    over: "{{ plan.output.watchers }}"
    branch:
      kind: agent
      spec:
        prompt_file: prompts/pm/monitor.md
        tools: [/* tape/oracle/macro allowlist */]
        max_turns: 20

  - id: scribe
    kind: join
    from: [exec, monitor]
    reduce: { type: postmortem }
    run: pm_desk.scribe                     # writes vault + may emit next-run trigger artifact

edges:
  - { from: prepare, to: seed }
  - { from: seed, to: directive }
  - { from: directive, to: dq }
  - { from: dq, to: dd, condition: "$.candidates.length > 0" }
  - { from: dd, to: eval }
  - { from: eval, to: plan, condition: "$.advance == true" }
  - { from: plan, to: freeze, port: proposes_live }
  - { from: freeze, to: exec, port: approve }
  - { from: exec, to: scribe }
  - { from: plan, to: monitor }             # monitors arm with plan
  - { from: monitor, to: scribe }

gates:
  freeze:
    id: freeze
    channel: telegram
    approvers: [joe]
    dual_control: true
    on_timeout: shelve
    timeout_s: 86400

triggers:
  - kind: cron
    schedule: "0 7 * * 1-5"
  - kind: webhook
    name: pm-desk-tick
    events: ["manual", "source.alert"]

notify:
  channel: telegram
  on: [gate, failed, completed]
```

**Feedback loop (scribe → prepare):** not a cycle in one run. `scribe` script writes `desk_state` and optionally enqueues:

```bash
hermes workflow run pm_desk_v0 --input-file runs/prev/artifacts/next_input.json
```

via cron `context_from` or webhook.

---

## 4. Cron invocation

### Phase 1 (no cron schema change)

```bash
# ~/.hermes/scripts/run_pm_desk.sh
#!/usr/bin/env bash
set -euo pipefail
hermes workflow run pm_desk_v0 --input '{}'
```

```bash
hermes cron create "0 7 * * 1-5" \
  --name pm-desk-daily \
  --script run_pm_desk.sh \
  --no-agent \
  --deliver telegram
```

(`no_agent` + script stdout delivery optional; workflow may notify itself.)

### Event / webhook

```bash
hermes webhook subscribe pm-desk-tick \
  --prompt "ignored-if-script" \
  --script "workflow_start_pm_desk.py" \
  --deliver telegram
```

`workflow_start_pm_desk.py` reads JSON stdin, calls `workflow.run(...)`, prints summary or SILENT.

---

## 5. Conversation tool call (agent)

```
user: run the pm desk paper pipeline for today
assistant: workflow_run(workflow="pm_desk_v0", input={})
→ { run_id, status: "awaiting_gate", gate_id: "freeze" } | RunEnvelope
```

Human replies in Telegram `approve wf_…` → gateway maps to `workflow gate`.

---

## 6. Python DSL equivalent (fanout sketch)

See `examples/minimal_workflow.py`.

---

## 7. Failure / resume examples

```bash
# driver crashed during dd fanout
hermes workflow status wf_123
hermes workflow resume wf_123 --retry-failed

# start from plan only (artifacts present)
hermes workflow resume wf_123 --from plan
```
