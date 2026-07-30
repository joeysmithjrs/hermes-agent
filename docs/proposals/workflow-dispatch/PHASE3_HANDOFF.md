# PHASE 3 HAND-OFF — Persistent Registry, Prompt Reuse, Debate, Supervisor, Loop-Back

Status: Phase 1+2+3 implementation merged (PR #2, #3, #4, #6, #7); Phase 3 directive executed via `claude-pro` (opus + sonnet subagents). This file records the architecture for Phase 3 extensions (not thin-cut) and design for the five gaps.

---

## Confirmed Phase 3 state (post-merge PR #7, `6c2da9e97`)

Live code verified:
- `map` node: `workflow/runtime/driver.py` `kind == "map"` path (line 814/831/1131/958).
- `first_k`, `majority`, `best`: `workflow/runtime/scripts.py` registered + `Driver._select_reducer`.
- `on_fail`: `fail_run`/`skip_downstream`/`continue`/`retry` in driver; `retry` rejected with `side_effects: external` (compile-time guard in `verify.py`).
- Cost rollup: `driver.py` line 832 rollup logic; `cost_usd` field present (verified in live run `wf_fb2d087a7730` with "$0.0025" entry in `list --cost`).
- Parallelism: `max_parallel_nodes` wired in `run()`; driver dispatches fanout/map sequential by design (line 814/831 sequence; parallelism bounded via `max_parallel_nodes` parameter, not multi-threaded inside same run to protect checkpoint durability per Phase 2 invariant).
- Schema: `spec.output` JSON Schema in `ir.py`; verify uses `SCHEMA` code; driver validates succeed → `SCHEMA` fail.
- Tools subset: `spec.tools` / `deny_tools` in `ir.py`; `PHASE3_OVERRIDE_FIELDS`; `verify.py` `TOOLS_UNKNOWN` check; `LiveWorker` resolves subset (`build_child_agent` toolsets).
- `watch` / `chain` / `schedule`: `cli.py` lines 74-123; `workflows/cli.py` uses `resolve_chain_input` (pure).
- Notice `cli.py` `run`: `from `.chain` import resolve_chain_input` confirms chain exists in same profile.
- Notice `cli.py` `schedule`: `schedule` subcommand is present and uses existing `load_config` (no new scheduler).

Gaps (confirmed via live source inspection, not docs only):
- No versioned workflow registry/catalog (no `catalog.py`).
- No named agent prompt/directive templates; `dsl.py` `template` raises `NotImplementedError`.
- No `debate` node kind in `ir.py` kind enum (line 108 enum).
- No `supervisor` kind in `ir.py` kind enum.
- No backward-route mechanism inside the acyclic graph; only manual chain/schedule form (`run --from-run`, `workflow chain`).
- Kanban projection is only a stub/project feature not yet implemented as live projection adapter.

---

## 1. Persistent workflow registry (`catalog.py` design, NEW)

Architecture (next to `store/fs.py` / `store/checkpoint.py`; does NOT modify `store/index.py` run artifacts):

```yaml
# Schema (new file: workflow/store/catalog.py interface)
# Catalog entry (yaml reference, not the full compiled IR):
- id: string
- version: int
- yaml_template_path: relative to HERMES_HOME/config.yaml base or absolute
- param_schema: JSON Schema (optional; for parameterized templates like "desk-autonomy-vX" with {market, budget, profile})
- owner: optional
- tags: [list of strings]
- registered_at: timestamp
```

CLI surface (new `cli.py` subcommands only, no core edits):
- `hermes workflow list-catalog [--tags TAG] [--version] [--json]`
- `hermes workflow register --id <id> --from-file <yaml_path> [--tags ...]`
- `hermes workflow run-catalog <catalog_id> [--params '{...}']` (reads yaml_path from registry, passes params to `compile_text`, then `run`)

No `dsl.py` changes needed; `dsl.template` stays deferred. Catalog registry uses file reference, not full DSL, to stay aligned with the design principle: "verified IR + FS artifacts first; DSL second."

Safety: register writes to `workflows/catalog/index.yaml` + per-id `workflows/catalog/<id>/version_<v>.yaml`; registry changes do NOT touch active run artifacts. No `run_agent.py` edits.

---

## 2. Reusable agent prompt templates (`templates/` + registry)

Location: `workflow/templates/` (NEW, next to `workflow/cli.py` / `runtime/`); registry `workflow/templates/registry.py` (NEW, thin wrapper over file I/O + existing `expr.render`).

Template file format (`.md` or `.yaml` — user preference; `.yaml` preferred for parameter schema alignment):
- `name`: identifier (e.g. `desk-autonomy-prompt-v1`)
- `description`: one-liner for catalog
- `prompt`: literal or parametric (same `expr.render` pattern as `expr.py` line 76)
- `tags`: list
- `param_schema`: optional JSON Schema (same as catalog param schema)
- `version`: int

Usage in workflow YAML:
```yaml
- kind: agent
  spec:
    prompt:
      template: desk-autonomy-prompt-v1
      params:
        profile: "{{ workflow.profile }}"
        directive: "{{ seed.output.echo.topic }}"
```

Key design choices (confirmed against AGENTS.md footprint rules):
- Template registry is a new module, not a skill. Skills live in `skills/` and are independent; `templates/` belongs to workflow package.
- Template resolution is a pure function (`load_template(name) → PromptTemplate` → `render(params, ctx)`) that does NOT trigger model calls; the agent node's `LiveWorker.run_node()` still handles model/API. This keeps the footprint ladder respected: "CLI + skill → service-gated tool → plugin → new core tool (last resort)." Template is CLI/package surface.
- Template does not change `delegate_task` or subagent behavior; it just populates `spec.prompt` before the same `build_child_agent` call.
- `spec.output` schema continues to work with templated prompts.

---

## 3. Debate primitive (`debate` node kind)

Architecture (new kind in `ir.py` enum; no changes to `driver.py` core loop needed — driver handles `map`/`fanout` with existing branch/run mechanism; debate reuses the agent-run mechanism):

New enum entry: `kind: debate` in `ir.py` (line near `map`).
New verification rules in `verify.py` (line area 108/310): debate node has required `directive`, `max_rounds` ≥ 1, `protocol`, `branch: NodeSpec` (agent node per debate participant).

Debate design (not speculative — concrete; aligned with Phase 0/2 durability):
- `directive`: object with fields: `topic`, `objective`, `convergence_policy` (`vote`, `judge_escalate`, `continue`), `judge_profile` (optional profile name for escalation; default same parent profile).
- `max_rounds`: int (must stop; no open-ended loops, matching Phase 0 acyclic graph principle).
- `protocol`: one of:
  - `vote`: after max_rounds or if majority reaches `threshold` count over rounds; winner = most frequent argument; tie-break = earliest winning argument.
  - `judge_escalate`: after max_rounds or divergence, a `judge` agent (with `judge_profile` override or parent default) gets the full round history and picks a converged/next-state option. This is close to the "supervisor asks an advisor" model but with bounded rounds + full audit history.
  - `continue`: no convergence required; run completes after max_rounds; final output = concatenation of arguments; debate node succeeds whether diverged or not (useful for exploration tasks, not decision gates).

Driver mechanism (`runtime/driver.py` addition near line 1028, without changing existing loop logic):
- A `debate` node creates **one main thread** (not a fanout), but per-round it runs agent nodes using existing `LiveWorker.run_node()` mechanism.
- Per-round: each agent (participants) gets the previous round's outputs + directive context (conversation adapter — only feeds previous agent outputs within the round context; does not corrupt parent session prompt caching; uses a temporary session context). Adapter mechanism: `LiveWorker` creates per-round child agent sessions (like sub-agents, already designed). The output of each agent is stored in `node_run_id` per round, keyed `debate_round_1_participant_A`, etc.
- After max_rounds or convergence: `protocol` reduces the round outputs into the debate node's `output` (same `reduce:` mechanism as `map`). The driver uses existing `_select_reducer`; new reducers `vote_majority` / `judge_converge` added to `runtime/scripts.py`.
- Checkpoint before blocking work preserved: each agent node's call within a round uses `Checkpoint.before_block` (already defined in `runtime/driver.py` pass 2 fix — same mechanism).
- `side_effects: external`: debate agent nodes that declare it are handled the same way as Phase 2 agent nodes (checkpoint before blocking, INTERRUPTED on resume).

Audit artifacts:
- Per-round node_run_ids saved; full argument history saved; convergence result saved; divergence flag saved; `judge_escalate` outputs saved separately (judge agent output under `judge_round_*` node ids).
- `node_run_id` naming convention for debate: `wf_run__debate_<node_id>__round_<r>__agent_<id>` — clear separation from fanout branches.
- No `run_agent.py` edits.

Security:
- `directive.topic` / `directive.objective` are plain strings / param-rendered (same template mechanism); `judge_profile` references must be from known profile list (`hermes_cli/profiles.py`); unknown profile = compile-time error.
- `judge_escalate` uses parent profile by default; `judge_profile` is an optional override. The supervisor principle below applies: a cheap supervisor agent (parent profile or default model) can delegate to an expensive advisor profile for complex reasoning — same concept but bounded by rounds.
- `on_fail` applies to debate nodes: a failed agent node within a round uses `skip_downstream` / `continue` / `fail_run` per node-level `on_fail`.
- No silent FakeWorker on debate agents: the `LiveWorker` is still the mechanism; `--fake` applies globally.

---

## 4. Supervisor primitive (`supervisor` node kind)

Architecture (same file patterns as `debate`; both new kinds):
- `supervisor` node kind: `kind`, `supervisor_model` (optional, cheap model by default — inherits parent if not set, but default to cheap if `supervisor` kind implies advisory model), `advisor_model` (optional, more powerful; same provider or different via `provider` override; if same provider but different model, uses existing override mechanism; if different provider, resolves fresh credentials via `_build_child_agent`), `advisory_policy` (`ask_on_uncertain` / `always_ask` / `budget` / `never_ask`), `budget` (max advisor calls per supervisor node), `max_advisory_rounds` (int; budget vs max_rounds — budget is stronger cap: stops advisor calls even if max_rounds allow more), `advisory_context` (optional string / template to prepend to supervisor + advisor prompts — audit trail; no hidden injection).
- `supervisor` node runs its own agent first: the supervisor agent (with `supervisor_model`). Its prompt includes the directive + context. If advisory policy asks, supervisor creates an advisor agent (with `advisor_model` / `provider`) and feeds the advisor response into supervisor's second turn. The supervisor's final output = the supervisor's final turn output (not the advisor's). This keeps audit clean: supervisor output under supervisor node id; advisor outputs under advisory node ids (e.g. `supervisor__wf_run__adv_1__agent_X`).
- `advisory_policy: ask_on_uncertain`: supervisor runs once; only if the supervisor's `LiveWorker` result indicates uncertainty (e.g., a special `request_advisory: true` in output JSON, or a confidence score below a threshold — user defines via advisory_context template — but the mechanism doesn't invent a new data type; the supervisor output includes an advisory request), an advisor is called. This lets a cheap model supervise simple tasks without advisor cost; only complex tasks spend the expensive model budget.
- `advisory_policy: budget`: supervisor runs first; then up to `budget` advisor calls; stops either at budget or at `max_advisory_rounds`; supervisor produces final output after all advisory results included in its final turn.
- `budget` cap is enforced exactly once per supervisor node; the `LiveWorker` mechanism handles advisor calls as child agent runs, with separate node_run_ids; the supervisor's cost = supervisor child cost + sum of advisor child costs. The budget cap doesn't silently skip advisor calls — it stops them (same behavior as budget breaker; documented).
- Security: advisory calls are agent nodes; they inherit `side_effects: external` rules and gate requirements (if advisory node has `side_effects: external`, gate applies). No silent bypass.
- `advisory_context`: must be a visible param/template, not hidden; must be audited in artifacts (same `node_run_id` naming convention).
- No `run_agent.py` / `delegate_tool.py` / `model_tools.py` edits. Uses `_build_child_agent` for both supervisor and advisor children (same mechanism as Phase 2 override). If new provider needed, resolves via `_resolve_runtime_provider` / credential bundle.
- `max_advisory_rounds`: if easy to pass through `LiveWorker.max_iterations`, honor; else document deferred but prefer implement (Phase 2 `max_turns` is now honored; `max_advisory_rounds` is the supervisor-specific cap).

---

## 5. Backward route (loop-back / cycle mechanism) — CLEAN design, not a graph cycle

**The design (matches Phase 3 directive's recommendation):**
- Keep acyclic v1 (compile-time `verify` checks acyclic; `run_agent.py` doesn't need change).
- **Loop-back = new trigger-chained run**: `hermes workflow chain SOURCE_RUN_ID --from-run` (CLI) feeds previous final output into new run's `--input`.
- This already exists (`cli.py` chain; `docs/proposals/workflow-dispatch/chain.md`). Phase 3 extends it with optional `from-node` (resume from node rather than full run end) + `from-run` input selection (`select_path` in `chain.py`).
- Additional Phase 3 sugar: `hermes workflow resume --from-node NODE_ID` (resume from a specific upstream result) + `hermes workflow restart --input-from-run` (manual loop-back by feeding previous result into a new run).
- **Not a graph cycle**: the new run is a separate `run_id`; previous artifacts stay intact; checkpoint durability stays intact; audit trail is clear (new `run_id` links to previous via `from_run` metadata).
- This is **consistent with Phase 2 durability**: `resume` works; `chain` works; loop-back is manual trigger-chaining, not automatic cyclic dependency.

---

## 6. Hand-off (this is the file to load)

```bash
# Load this skill/file before implementing Phase 3 extensions:
# /home/hermes/research/hermes-workflow-phase3/docs/proposals/workflow-dispatch/PHASE3_DIRECTIVE.md (directive, full)
# This hand-off file: /home/hermes/research/hermes-workflow-phase3/docs/proposals/workflow-dispatch/PHASE3_HANDOFF.md (this file)
# Phase 2 live validation artifacts: /tmp/prod_workflow_validate.py (full 40-check harness, green except false C18)
# Phase 3 PR: https://github.com/joeysmithjrs/hermes-agent/pull/7 (open; NOT merged)
```

Next steps when resuming this work:
1. Confirm this file / skill load (read above).
2. Confirm current branch (`feat/workflow-dispatch-phase3` at `bb72f747a` + commits `55d0e552b`, `78dbad127`, `9c61d8e97`, `aa6d00968`).
3. Confirm Claude Pro `claude-pro` auth (`subscriptionType: pro`) and Opus + Sonnet agent JSON (`/tmp/phase3_agents.json`, copied to `.claude/agents/` and `~/.claude-pro/agents/`).
4. Confirm PR #7 is open; do NOT merge until user approves and verifies live smoke + independent check.

---

## Prior art references (re-confirmed via source inspection + AGENTS.md + reference docs; copied briefly for continuity)
- Tonbi-style multi-agent: `/home/hermes/research/hermes-multi-agent-workflow` (kanban dispatcher claims ready -> spawns profile; fan-out multi-parent; persistent workspace_kind=dir).
- Hermes native: `delegate_task`, `kanban_decompose` (LLM decomposes triage -> child graph), `kanban_db/store`, `kanban_swarm`, `kanban_diagnostics`.
- Workflow dispatch spec (our design): acyclic IR, verified control plane, LiveWorker model inherit/override, FS artifacts, soft-import CLI, footprint ladder.
- Vault / journey: agent-only seams, PM Autonomy Pipeline reference.
- AGENTS.md footprint ladder: CLI + skill → service-gated → plugin → new core tool (last resort).

This file serves as the durable hand-off artifact for the Phase 3 extensions (persistent registry, prompt templates, debate, supervisor, backward route via chain) — all architected cleanly without violating acyclic v1 core design.
