# DIRECTIVE — PM Desk idea-gen v3 + plan SCHEMA reliability (ONE PR)

**To:** Claude Code Pro (Opus orchestrator; optional Sonnet agents)  
**From:** Joe / Hermes operator  
**Repo / worktree:** `/home/hermes/research/hermes-pm-desk-idea-gen-v3`  
**Remote:** `origin` = `joeysmithjrs/hermes-agent` (fork only — never upstream Nous)  
**Base:** `origin/main` @ current HEAD when you started  
**Branch:** `feat/pm-desk-idea-gen-v3` (already checked out)  
**Package:** `optional-projects/pm-desk/` (+ minimal Hermes `workflow/` SCHEMA fix if needed)  
**Auth:** Claude **Pro OAuth only** (`CLAUDE_CONFIG_DIR=~/.claude-pro`). No OpenRouter/CCR.

---

## 0. Mission (one sentence)

Ship **one PR** that fixes morning **idea generation** (mission × arena fanout + code/stat exploit taxonomy) and makes the **plan node reach `paper_gate`** when a valid ExecutionPlan exists (SCHEMA / envelope reliability), with tests green and a real PR opened (do not merge).

---

## 1. Why (live evidence — do not re-litigate)

Live run `wf_9e6e868d97f1` (post PR #15):

- Seed was **cross-family** (FV + explore + coherence + buildout) → shallow unrelated candidates.
- Joe’s intent for fanout: **same rough mission / edge method**, **different angles** — now preferred as **different Polymarket arenas/sectors**.
- Plan agent wrote a **valid** `execution_plan.json` under the workspace, then put **markdown** in Hermes `text` → SCHEMA fail → `paper_gate` skipped → `plan from-run` blind.
- Ethos: seams where **agentic DAG + webhooks + deterministic code** extract Polymarket edge — not pure HFT, not pure hopium. Code/stat exploits often want a **daemon/repo**, with Hermes only on wake.

---

## 2. Non-negotiables

1. **Paper only** — no wallet, orders, signing, live execution paths.  
2. **Footprint:** almost all work under `optional-projects/pm-desk/`. Hermes core only if SCHEMA coerce genuinely needs it (`workflow/runtime/driver.py` and tests). No `run_agent.py`. No root package.json infection.  
3. **Grok nodes** stay `provider: xai-oauth` + bare `grok-4.5`.  
4. **No per-agent `max_turns` / `budget_usd`** (make-it-work first).  
5. **Workspace** `pm-desk` stays; agents still write stage artifacts under `{{ workspace.run_dir }}`.  
6. **`proposed_buildouts`** never auto-provisioned / never silent Claude Code spawn.  
7. Incremental commits (~few hundred LOC each). ASCII commit messages.  
8. Real verification: `npm run check` in package; `hermes workflow validate` on generator YAML; independent pytest if you touch `workflow/`.  
9. Open PR to **fork main**; **do not merge**.  
10. Node ≥ 24 on PATH: prefer `$HOME/.local/node-v24.18.1-linux-x64/bin`.

---

## 3. Deliverables (all three resolutions)

### A. Arenas + `mission_x_arena` seed mode (primary idea-gen fix)

**Add** `optional-projects/pm-desk/taxonomy/arenas.yaml` (versioned):

Each arena roughly:
```yaml
id: pop_culture   # politics, tech, sports, crypto, macro_sched, geo, other_midcap, …
title: …
description: …
# Machine filters for discover (not vibes-only):
gamma_tag_any: […]      # optional
slug_includes: […]      # optional
slug_excludes: […]      # optional
question_regex: …       # optional
min_liquidity_usd: …    # optional soft guidance for agents
examples: […]
bans: […]               # e.g. mega index beta if desired
```

Ship a solid starter set (≥6 arenas): `pop_culture`, `politics`, `tech`, `sports`, `crypto`, `macro_sched` (plus more if natural).

**Compiler** (`src/taxonomy/index.ts` + CLI):

- Keep existing mode as `stratify_families` (default for backward compat OR make `mission_x_arena` the **recommended default for morning** — prefer **mission_x_arena as default for `taxonomy compile` used by mornings**, but keep stratify callable via flag).
- New mode **`mission_x_arena`**:
  1. Pick **one mission family** (weighted among active method families suitable for multi-arena fanout — at minimum `fair_value_research`; also allow `microstructure_event`, `intra_poly_coherence`, `primary_source_sniper` / resolution-style if clean). Prefer missions with `always_reserve_slot` or explicit `mission_eligible: true` on family.
  2. Sample **N arenas** without replacement (N = `maxCards`, default 3–4).
  3. Emit **N cards** that share the same mission card template but each is bound to one arena:
     - `card_id` stable: e.g. `{mission_card_id}__{arena_id}` or keep base card_id and set `arena_id` field (prefer **explicit `arena_id` + `mission_card_id` fields** on SeededCard).
     - `directive` = mission directive + **hard arena scope block** (must not leave arena; use discover filters; reject empty arena honestly).
     - Include arena filters in the seeded card object so prompts/tools can use them.
  4. Seed hash must include mode + mission + arena ids + taxonomy/arena versions so runs are reproducible.
  5. Optional: still allow **one** `explore_seed` or `tool_and_build_proposals` / `code_stat_exploit` slot when `maxCards` allows and a flag `--include-explore` / weighted coin — document; default morning can be pure mission×arena for clarity.

**CLI:**
```bash
npx tsx src/cli/pm-desk.ts taxonomy compile \
  --mode mission_x_arena \
  --mission fair_value_research \    # optional override; else weighted pick
  --max-cards 3 \
  --json
# also: --mode stratify_families (legacy)
# also: taxonomy arenas list
```

**Workflow / prompts:**

- Update `pm-directive-v1` (and DQ/DD lightly) so fanout branches receive:
  - `{{ branch }}` / params including **arena** (id, title, filters, bans)
  - mission family + base card
  - **Do not leave the arena.** Prefer pm-desk discover with filters; if no liquid market in arena, structured reject — do not jump to CPI because it’s easier.
- Fanout `over` still works on `directive_cards` list; each card carries arena.
- Generator YAML comment block documents `mission_x_arena` as the intended morning seed.
- First-live / README / skill-facing docs in package if present: one short section on seed modes.

**Tests:** taxonomy unit tests for:
- mission×arena emits N cards same mission different arenas
- seed reproducibility
- arena validation (bad ids, empty file)
- stratify mode still works
- reserved behavior if you keep explore hybrid

Bump **taxonomy version to 3** when card/arena semantics change; arenas file own version field.

---

### B. Taxonomy family: `code_stat_exploit` (code-first / statistical)

Add active family in `taxonomy/cards.yaml` (v3):

**Family** `code_stat_exploit`:
- Description: design **daemon/repo/streaming** exploits (jumps, vol, bounds, schedule windows, nowcast calibration). Hot path is often **not** Hermes cron every morning — Hermes wakes on trigger or receives a build proposal.
- Cards (examples — refine wording, keep paper_only):
  1. `jump_mean_reversion_daemon` — universe by arena/tag; define jump threshold, false-positive policy, paper signal; when to call Hermes for FV/news vs pure paper auto.
  2. `streaming_coherence_bounds` — related-market arithmetic as code checker.
  3. `post_print_state_machine` — coded T0 window 5–30m.
  4. `nowcast_bucket_calibration` — historical RMSE → bracket probability (the CPI gap from live run).
- Deliverable shape: prefer **`proposed_buildouts[]`-ready** blocks (interface, validation_plan, spawn_recommendation `none|claude_code_pro_after_approval`, approval_required true) + optional monitor specs only if truly script-first and safe.
- Weight modest (1–2); **not** always_reserve unless you also want it in mission×arena mission list (usually **not** a multi-arena FV mission — appears in stratify or as optional 4th card).

Update taxonomy tests for family presence + compile eligibility.

Update execution-plan / DD prompts briefly: if candidate is code-stat, emphasize buildout over fake narrative edge.

---

### C. Plan node SCHEMA reliability → reach `paper_gate`

**Root cause:** plan model writes valid JSON via `write_file` to workspace, then final assistant `text` is markdown → `_schema_instance_candidates` never sees the plan → SCHEMA fail → gate skipped.

**Fix both layers (belt and suspenders):**

1. **Prompt (`pm-execution-plan-v1`)**  
   - Final model message MUST be **only** the ExecutionPlan JSON object (no markdown bullets, no prose wrapper).  
   - Still `write_file` to `{{ workspace.run_dir }}/execution_plan.json` and cross-run last_* files.  
   - Explicit: “If you write the file first, your chat text must still be the same JSON.”

2. **pm-desk `plan from-run`**  
   - Also look for workspace path:  
     `$HERMES_HOME/workflows/workspaces/pm-desk/runs/<run_id>/execution_plan.json`  
     (and/or `last_execution_plan.json` only if run_id matches summary).  
   - Prefer valid workspace plan if node envelope fails parse.

3. **Hermes SCHEMA coerce (if needed for gate)**  
   - In `workflow/runtime/driver.py` `_schema_instance_candidates` (or plan-node post-hook only if cleaner): when validation fails, optionally try reading known workspace artifact paths from ctx if present — **only if** you can do it cleanly without breaking hermetic tests.  
   - Prefer fixing so **successful candidate is the parsed JSON from text** via prompt discipline + extracting JSON from fenced blocks if `_parse_loose_json` doesn’t already.  
   - Check `_parse_loose_json` — extend to pull first `{...}` object from markdown if safe.  
   - Add/extend `tests/workflow/` SCHEMA tests for: markdown-wrapped JSON still validates; pure markdown fails; agent envelope with JSON in text passes.

4. **Workflow**  
   - Keep plan `spec.output` schema.  
   - Ensure failed plan is retriable only if you mark SCHEMA retriable (currently False) — better to **pass** than retry loops.  
   - Document operator: after merge, install-prompts --force.

5. **Optional helper**  
   - `pm-desk plan salvage --run-id` that promotes workspace plan into a validated file for gate bridging without full re-run — nice-to-have if from-run already covers it.

---

## 4. Implementation order (commits)

Make **separate commits** roughly:

1. `feat(pm-desk): arenas.yaml + arena loader/validation`  
2. `feat(pm-desk): mission_x_arena seed compiler + CLI flags`  
3. `feat(pm-desk): directive/DQ prompts bind arena scope`  
4. `feat(pm-desk): taxonomy v3 code_stat_exploit family`  
5. `fix(pm-desk): plan from-run reads workspace execution_plan.json`  
6. `fix(workflow): SCHEMA coerce loose JSON / markdown-wrapped plan objects` (only if needed)  
7. `docs(pm-desk): seed modes + code-stat runtime split`  
8. Tests woven into the commits above when natural.

---

## 5. Verification (you run; operator will re-run)

```bash
export PATH="$HOME/.local/node-v24.18.1-linux-x64/bin:/opt/hermes-agent/venv/bin:$PATH"
cd /home/hermes/research/hermes-pm-desk-idea-gen-v3/optional-projects/pm-desk
npm ci   # real native build — NOT --ignore-scripts
npm run check

# seed smoke
npx tsx src/cli/pm-desk.ts taxonomy compile --mode mission_x_arena --max-cards 3 --json | head -80
npx tsx src/cli/pm-desk.ts taxonomy compile --mode stratify_families --max-cards 4 --json | head -40

# workflow
hermes workflow validate workflows/pm-morning-generator-v0.yaml

# if workflow/ touched:
cd /home/hermes/research/hermes-pm-desk-idea-gen-v3
pytest tests/workflow -q --tb=line
```

Fixture: construct a fake run dir with markdown plan text + workspace `execution_plan.json` → `plan from-run` succeeds.

---

## 6. PR

```bash
git push -u origin HEAD
env -u GH_TOKEN -u GITHUB_TOKEN gh pr create -R joeysmithjrs/hermes-agent --base main \
  --title "feat(pm-desk): mission×arena idea-gen, code_stat family, plan SCHEMA path" \
  --body "$(cat <<'EOF'
## Summary
Improves morning idea generation (same mission × different Polymarket arenas), adds code/statistical exploit taxonomy, and fixes plan SCHEMA/workspace so paper_gate can open.

## Changes
- arenas.yaml + mission_x_arena compile mode (stratify_families retained)
- directive prompts: hard arena scope
- taxonomy v3: code_stat_exploit family (daemon/buildout-shaped)
- plan from-run + SCHEMA: recover valid ExecutionPlan from workspace / loose JSON

## Test
- npm run check (pm-desk)
- hermes workflow validate generator
- pytest tests/workflow (if touched)

## Non-goals
- No live trading
- No auto Claude Code spawn
- No merge (Joe merges)
EOF
)"
```

Print PR URL. **Do not merge.**

---

## 7. Out of scope

- Standing up real vol-harvest daemons in production  
- Enabling Hermes webhooks  
- Changing SuperGrok routing  
- Multi-DD fanout graph redesign  
- Fixing unrelated flaky e2e `source_market_divergence` silent path unless one-line obvious  

---

## 8. Done when

- [ ] mission_x_arena compile produces N same-mission different-arena cards  
- [ ] arenas.yaml loaded/validated  
- [ ] code_stat_exploit in taxonomy v3 + tests  
- [ ] prompts bind arena; generator still validates  
- [ ] plan from-run finds workspace execution_plan when node text is markdown  
- [ ] SCHEMA path does not fail a node that emitted valid plan JSON (text and/or recoverable artifact)  
- [ ] `npm run check` green  
- [ ] commits on branch + PR open to fork main  
- [ ] short summary: PR URL, commits, residual risks  

**BEGIN IMPLEMENTATION NOW.** Use Task/agents if helpful. Prefer small verified commits. Stay on Claude Pro.
