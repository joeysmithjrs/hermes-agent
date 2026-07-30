# AUDIT — Workflow Dispatch Spec Package

**Auditor:** GLM-5.2 only (z-ai/glm-5.2) · **Date:** 2026-07-29
**Scope:** spec audit + corrections. No runtime implementation.
**Method:** read all 9 proposal docs; verified claims against Hermes source
(`tools/delegate_tool.py`, `tools/async_delegation.py`, `hermes_constants.py`,
`hermes_cli/main.py`, `cron/jobs.py`, `hermes_state.py`, `gateway/`,
`toolsets.py`, `AGENTS.md`).

---

## Executive verdict

**SHIP-WITH-FIXES.** The architecture is sound and well-aligned to the AGENTS.md
Footprint Ladder (verified: `AGENTS.md:182` "The Footprint Ladder"), to verified
Hermes primitives (`delegate_task`, cron `no_agent`/`script`/`context_from`,
`gateway/delivery.py`, `hermes webhook subscribe`), and to a defensible deferral
of kanban. The IR is appropriately orthogonal and the "verified IR + deterministic
driver, control flow is not an LLM loop" thesis is correct.

But the spec as written had **one P0** (a store-path collision that makes the
Phase 1 fanout acceptance criterion literally impossible) and a cluster of
**P1s** where the docs overstate what the Phase 1 inherit path can enforce
(tool allowlists / profile isolation), leave dual-control as advisory rather
than verifier-enforced, allow arbitrary-code `run:` resolution, and leave
side-effecting agent nodes re-executable on resume. All P0/P1s are patched
in-place below; P2/P3s are documented as corrections. No rethink needed — the
fixes are surgical and preserve the design's shape.

---

## Findings table

| # | Sev | Axis | Finding | Disposition |
|---|-----|------|---------|-------------|
| F1 | **P0** | Correctness/Eff | Store layout keys node bodies by `node_id` (`nodes/<node_id>/output.json`); a `fanout`/`map` node's N branch executions overwrite each other. Breaks Phase 1 acceptance #2. | **Patched** design §4.3, §2.6 → keyed by `node_run_id`. |
| F2 | **P0→P1** | Security/Impl | Phase 1 inherit path (`delegate_task`, in-process `ThreadPoolExecutor`) **cannot honor** `tools`/`model`/`profile`/`max_turns`/`workspace`; `delegate_task(goal,context,tasks,max_iterations,role,background,parent_agent)` takes none (verified `tools/delegate_tool.py:2426`). `HERMES_HOME` is process-wide (set once by `_apply_profile_override`, `hermes_cli/main.py:474`; `get_hermes_home` reads env, `hermes_constants.py:107`). Spec implied tool allowlists/profile isolation work from Phase 1. | **Patched** design §6/§7/§8, api §2.4, phases.md: Phase 1 verifier rejects override-only fields; enforcement is Phase 2 (subprocess override path). |
| F3 | **P1** | Security | Dual-control is advisory: verifier only checks that *existing* gates have channel/approver. Nothing forces a gate before a live/side-effecting-tool node. Author wires `exec`+`trade_live` with no gate → no human checkpoint. | **Patched** design §5 gates row: live-tool node must have a gate on every inbound path or reject. |
| F4 | **P1** | Security | `run:` resolves arbitrary dotted callables (`os.system` is importable). `workflow_run` tool exposed to the chat agent → arbitrary Python exec. | **Patched** design §5/§8, api §2.2: `run:` must be registered/allowlisted. |
| F5 | **P1** | Efficiency/Correct | Fanout `over:` is a **runtime list**; the verifier's "worst-case fanout × cost" check is unverifiable without a declared cap. A seed returning 10k branches → 10k agent nodes. | **Patched** design §5, api §2.2: required `max_branches`; runtime hard-fail `CARDINALITY`; per-branch budget check. |
| F6 | **P1** | Correctness | Side-effecting agent nodes (e.g. `exec` placing trades) are requeued `running→ready` on resume → **double execution** of external side effects after a crash. | **Patched** design §4.4: `side_effects: external` → never auto-requeued, resumes to `failed` (INTERRUPTED), needs explicit `--retry-failed`. |
| F7 | **P1** | Correctness | Run status enum lacks `awaiting_gate`/`paused`, yet gates "park the run" and api §11 error table uses those run statuses. A gated run with succeeded+pending nodes had no defined status. | **Patched** design §2.5/§2.6: added `awaiting_gate`/`paused` to run status; distinguished from `partial`. |
| F8 | **P1** | Security | `on_timeout: approve_auto` allowed regardless of `dual_control`. A dual-control gate that auto-approves on timeout is self-contradictory. | **Patched** design §5, api §2.5: hard-reject `approve_auto` when `dual_control:true`. |
| F9 | **P1** | PM fitness | §11 claims monitors may "abort/reduce/hold" `exec` mid-flight via "gate-conditional edges back to exec." v1 edges fire on *completion*, not mid-run; no control-signal primitive exists. Overclaim. | **Patched** design §11: v1 monitors observe+report only; mid-flight abort deferred to Phase 3. |
| F10 | **P1** | Correctness | Template/`over`/prompt forms inconsistent across docs: `{{ seed.branches }}` (bare) vs `{{ seed.output.branches }}` (`.output`); `prompt: {file:}` vs `prompt_file:`. | **Patched** design §5/§3.2, api §2.2/§2.4: canonicalize `node.output.field` (bare = shorthand); `prompt_file` documented as YAML shorthand. |
| F11 | **P1** | Efficiency | `index.sqlite` re-implements journal-mode/WAL/NFS handling; `hermes_state.py` already solves this with private helpers (`_on_disk_journal_mode`, `_apply_macos_checkpoint_barrier`). NFS/FUSE failure modes are subtle. | **Patched** design §4.3: must reuse/extract `hermes_state` sqlite hardening, not re-derive. |
| F12 | **P2** | DRY/Reuse | Trigger-chain (scribe→new run) reinvents a manual `--input-file` plumbing when cron `context_from` (verified `cron/jobs.py:1085`) already chains "most recent output of another job." | Documented (residual Q): prefer `context_from` for cron-triggered chains. |
| F13 | **P2** | Correctness | `reduce.type` vs `run:` ambiguity: examples supply both `reduce: {type: concat}` and `run: demo.concat_summaries`. Unclear if `type` selects a built-in or just labels a custom `run`. | **Patched** api §2.2 note: `type` selects built-in reducer; custom reducer uses `run:`. |
| F14 | **P2** | Correctness | `reduce` types inconsistent: `first_k`/`majority` referenced in design §4.1 and test-plan but absent from the reduce-type list; short-circuit (`first_k`) leaves remaining branches running (orphan budget). | Documented (residual Q): short-circuit must cancel remaining branch node-runs. |
| F15 | **P2** | Upstream | Override path claims "uses the same low-level construction `_run_single_child` uses — no second spawning implementation." `_run_single_child` takes a *pre-built* child (`tools/delegate_tool.py:1791`, "Run a pre-built child agent"); child *construction* is separate glue. Reusable public construction helper not confirmed. | Documented (residual Q): confirm an importable child-construction helper before claiming zero core touch. |
| F16 | **P2** | Security | "Network denied unless `allow:[network]`" is unenforceable by a Python parent (subprocess can open sockets without OS-level isolation). Overclaim. | **Patched** design §8: honest scope — declaration gate, not capability wall; OS-level isolation is Phase 3. |
| F17 | **P2** | Correctness | `run_id` = `wf_<short>`; collision caught by sqlite index *after* the run dir is created. | Documented (residual Q): make `run_id` uuid4-based so collision is negligible. |
| F18 | **P2** | Security | "Secret-bearing tools" referenced but no registry/tag set defines which tools carry secrets. | Documented (residual Q): define secret-tool tag set. |
| F19 | **P3** | Nit | api §2.4 NodeSpec used stale model id `anthropic/claude-sonnet-4`. | **Patched** → `claude-sonnet-5`. |
| F20 | **P3** | Upstream | A new gate-signal HTTP route (`/webhooks/workflow-gate/...`) is a gateway touch beyond the "gateway optional P2" framing. | Documented: gate signal via CLI/Telegram in P1–P2; HTTP route is Phase 3. |

---

## DRY / reuse map vs existing Hermes primitives

| This feature builds… | Hermes primitive (verified path) | Verdict |
|---|---|---|
| worker leaf exec (inherit) | `tools/delegate_tool.py:delegate_task` (sig at :2426); bg on `ThreadPoolExecutor` (`tools/async_delegation.py:45,49`); pause `is_spawn_paused` (`:165`); depth `delegation.max_spawn_depth` (`:468`) | **Reuse justified.** Inherit path is zero-core-touch. Honest about override gap (F2). |
| worker per-node model/tools/profile (override) | child construction glue (`_run_single_child` takes pre-built child, `:1791`) | **Justified new shim**, but reusable public construction helper unconfirmed (F15). Phase 2. |
| profile / HERMES_HOME isolation | `get_hermes_home` (`hermes_constants.py:107`) + `_apply_profile_override` (`hermes_cli/main.py:474`) | **Process-boundary required** — in-process threads share one HOME (F2). Not a free reuse. |
| scheduled invocation | cron `no_agent`+`script`+`context_from` (`cron/jobs.py:1022,1085`) | **Reuse justified** (shell-out P1). `context_from` should serve trigger-chains (F12). |
| notify | `gateway/delivery.py` (exists); `hermes webhook subscribe` (`hermes_cli/webhook.py:162`) | **Reuse justified.** |
| run registry / sqlite index | `hermes_state.py` SessionDB pattern + WAL/NFS helpers (`:315,336`) | **Reuse the hardening helpers** (F11) — don't fork journal-mode handling. |
| kanban as bus | `gateway/kanban_watchers.py`, `hermes_cli/kanban.py`, `tools/kanban_tools.py`, `tests/hermes_cli/test_kanban_promote.py` | **Deferral justified** — kanban is sprawled across gateway+cli+tools; coupling risk is real. |
| tool registration | `toolsets.py` (`_HERMES_CORE_TOOLS:31`, `TOOLSETS:96`), `tools/registry.py` | Additive optional toolset entry; rebase-cheap. Acceptable. |

**New primitives truly justified:** `gate` (dual-control boundary), `fanout`/
`join`/`map` (graph structure delegate_task lacks), `NodeRunEnvelope`/checkpoint
(resumability), the verifier (graph validity). These are genuinely absent from
Hermes today. Not reinventing a dispatcher; extending the leaf executor with a
graph layer.

---

## Efficiency risks

- **Fanout cardinality bomb (F5):** was unbounded — runtime list, unverifiable.
  Patched via required `max_branches` + per-branch budget check at spawn.
- **Double execution on resume (F6):** side-effecting agent nodes. Patched via
  `side_effects: external` + no-auto-requeue.
- **Store path collision (F1):** N branch outputs under one path. Patched via
  `node_run_id` keying (also reduces nothing — it *enables* Phase 1 fanout).
- **Event-log volume:** per-node-execution `events.jsonl`, tool names only (no
  arg secrets) — acceptable. Fanout multiplies files by branch count; bounded
  by `max_branches`.
- **sqlite vs FS:** FS source-of-truth + sqlite index mirrors SessionDB; two
  writers (driver files + index) can diverge post-crash, but `status` falls
  back to `run.json` (authoritative) and `doctor` rebuilds index. Acceptable.
- **Profile processes:** each distinct `profile:` = a subprocess (override
  path). For PM desk that's ~1 (`exec` trader-paper) plus default-profile
  monitors in-process. No bound on distinct profiles/run — note for Phase 2.

---

## Changelog of patches applied

| Doc | Section | Change |
|---|---|---|
| design.md | §2.5 | Added `awaiting_gate`/`paused` to **run** status enum; distinguished from `partial` (F7). |
| design.md | §2.6 | `artifact_ref`/`events` keyed by `node_run_id`; RunEnvelope status line updated (F1, F7). |
| design.md | §3.2 | `over:` canonicalized to `{{ seed.output.branches }}` (F10). |
| design.md | §4.3 | Store layout keyed by `node_run_id` + P0 invariant note; sqlite must reuse `hermes_state` hardening (F1, F11). |
| design.md | §4.4 | Added side-effecting-agent-node resume rule (`side_effects: external`, no auto-requeue) (F6). |
| design.md | §5 | Verifier: `max_branches` required + runtime CARDINALITY; live-tool-must-be-gated; `run:` allowlist; `approve_auto` rejected when dual_control; template canonical form; `prompt_file` shorthand (F3,F4,F5,F8,F10). |
| design.md | §6 | Profile isolation honest scope: inherit path can't honor overrides; process boundary required; Phase 1 verifier rejects override-only fields (F2). |
| design.md | §8 | Script sandbox honest scope: declaration gate not capability wall; OS-level isolation Phase 3 (F16). |
| design.md | §11 | Monitors v1 = observe+report; mid-flight abort deferred to Phase 3 (F9). |
| api.md | §2.2 | Added `max_branches` (required), `side_effects`; canonical `over` form; `run` allowlist note; reduce.type-vs-run note (F4,F5,F6,F10,F13). |
| api.md | §2.4 | Model id → `claude-sonnet-5`; enforcement-phase note (inherit vs override); `prompt_file` shorthand (F2,F10,F19). |
| api.md | §2.5 | `on_timeout: approve_auto` hard-rejected when dual_control:true (F8). |
| phases.md | Phase 1 | Added inherit-path limitation section; store-path keying; `max_branches`+`side_effects` in P1; override enforcement = Phase 2; acceptance criteria #2–#5 expanded (F1,F2,F5,F6). |
| test-plan.md | §2 | Added 8 audit-invariant verifier + store/resume tests (F1,F3,F4,F5,F6,F8). |
| examples.md | §3 | PM skeleton: `directive` `max_branches:12`; `exec` `side_effects: external` (F5,F6). |

---

## Residual questions (post-patch)

1. **Trigger-chain via `context_from` (F12):** for cron-scheduled PM loops, does
   `context_from` ("most recent output of job X") suffice, or does the scribe
   still need an explicit `--input-file`? Prefer `context_from` to avoid new
   plumbing.
2. **`reduce.on_fail` + short-circuit cancellation (F14):** default `fail_join`
   recommended; `first_k`/`majority` short-circuit must cancel remaining branch
   node-runs (specify explicitly before Phase 3).
3. **Override-path construction helper (F15):** confirm an importable, reusable
   child-`AIAgent` construction helper exists before claiming zero `delegate_tool`
   edits. If not, the override path's "no second spawning implementation" claim
   weakens to "small additive kwarg" — acceptable but must be honest.
4. **`run_id` generation (F17):** uuid4-based so collisions are negligible, not
   index-caught-after-creation.
5. **Secret-tool tag set (F18):** define which tools are "secret-bearing" for
   the verifier's secret-scavenging reject (config list or tool metadata tag).
6. **Gate-signal HTTP route phasing (F20):** gate decisions via CLI/Telegram in
   P1–P2; the `/webhooks/workflow-gate/...` route is Phase 3, not P2.
7. **Model id format (api §2.4):** confirm `anthropic/claude-sonnet-5` (or
   bare `claude-sonnet-5`) matches Hermes' real provider/model registry before
   the verifier's unknown-model check ships.
8. **Profile billing attribution** (carried from original SUMMARY): child
   subprocess spend → `RunEnvelope.cost_usd`.

---

## Solid sections (no change needed)

- IR orthogonality (§2), the "control flow is code" thesis (§1), acyclic + trigger-chain cycle story (§5) — conceptually correct and the cleanest way to keep a run resumable.
- Upstream packaging: new `workflow/` package + soft-import CLI + default-off toolset is genuinely Footprint-Ladder-compliant (verified `AGENTS.md:182`).
- Kanban deferral rationale (§10) — verified kanban sprawl; coupling risk real.
- Cron shell-out reuse (§6 api) — verified `no_agent`/`script` exist.
- `delegate_task` reuse for the inherit path (§4.2) — verified; zero-core-touch and the right Phase 1 default.
