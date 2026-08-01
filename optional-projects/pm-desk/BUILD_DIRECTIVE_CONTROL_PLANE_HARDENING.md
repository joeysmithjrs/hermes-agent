# PM Desk Control-Plane Hardening — Implementation Plan

> **For Hermes / Claude Code Pro:** Implement task-by-task on a worktree off `main`. Open one PR on `joeysmithjrs/hermes-agent`. Do not merge. Do not run live trading. Paper only.

**Goal:** Close the gaps exposed by the first real morning → buildout → reopen → live-history cycle so the desk (1) presents decisions Joe can act on, (2) auto-does scrape/code/data work without nagging, (3) only gates Joe for true infra/spend/risk, and (4) never stops at the first broken path when public data still exists.

**Architecture:** Keep North Star (generate → research → plan → dual-control → provision monitors). Add a first-class **thesis reopen / continue** path after tools ship. Split **approval classes**: `joe_gate` only for money/credentials/external infra; **agent-auto** for code, scrapers, public data, hermetic harness runs. Encode **data-plane aggression** (multi-source, fail-loud, no fixture-as-truth) into harnesses and scout prompts. Edge-first briefing is a plan/render contract, not optional prose.

**Tech stack:** `optional-projects/pm-desk` (TS/Node 24), Hermes `workflow/` YAML + gate CLI, existing `research/cpi_nowcast`, workspace `pm-desk`, SuperGrok `xai-oauth` + OR for other models.

**Evidence base (this session):**
- `wf_e644355a7598` morning: mission×arena OK; plan OK; gate OK; proposed `cpi_nowcast_bucket_harness` instead of running missing math.
- Harness #17 shipped; agents still didn’t know to run it until we taught them ad hoc.
- Reopen `wf_20eba875ef75`: harness ran but on **fixtures** → fake 75% vs 42%; plan node freestyled schema; operator hand-repaired plan.
- Live history (manual, after Joe pushback): Cleveland `nowcast_year.json` N=154 → **p_bucket≈38% vs mid 42.5% → no_trade**. HTML regex loader was the lazy failure mode.
- Joe corrections: edge-first briefs; loop back after build; boil the ocean on data; **approve only for buy data / stand up infra**, not for coding/scraping.

---

## Gap inventory (all of them)

### A. Product / control-flow gaps

| ID | Gap | Why it hurt |
|----|-----|-------------|
| A1 | **No reopen loop** after buildout ships | Morning dead-ends at “propose harness”; trade never re-decided in-graph |
| A2 | **`proposed_buildouts` conflates** “need eng work” with “need Joe money/infra” | Joe gated on coding that should have been automatic |
| A3 | **Gate is one-size** (approve whole plan) | Approve empty monitors ≠ approve live-data spend; dual purpose confuses |
| A4 | **Thesis reopen was bespoke YAML on bare main**, not productized | Won’t exist next crisis unless shipped |
| A5 | **Parked morning + reopen = two gates** | Operator cognitive load; no linkage |

### B. Data-plane / “boil the ocean” gaps

| ID | Gap | Why it hurt |
|----|-----|-------------|
| B1 | **CPI live loader = fragile HTML regex** | Failed → fell back to fixtures → wrong trade signal |
| B2 | **Fixture results presented as if live-calibrated** | 75% vs 42% looked like edge; real was 38% vs 42% no_trade |
| B3 | **No multi-source failover** (JSON media URL, table scrape, FRED/BLS API) | Stopped at first error |
| B4 | **No “series provenance” on calibrate output** | Plan/research couldn’t hard-fail `source=fixture` for entry |
| B5 | **Agents not required to exhaust public endpoints** before `proposed_buildouts` | Buildout used as escape hatch for laziness |

### C. Agent briefing / Joe UX gaps

| ID | Gap | Why it hurt |
|----|-----|-------------|
| C1 | **Briefs tooling-first** by default | Joe has to extract EV himself |
| C2 | **No mandatory sell structure** on telegram_brief / buildouts | Inconsistent “why edge exists” |
| C3 | **Plan schema freestyle on reopen** | Gate opened on invalid desk plan; operator repaired |
| C4 | **Approval note vs decision content** | Approve recorded run, not a clear trade ticket |

### D. Tool awareness gaps

| ID | Gap | Why it hurt |
|----|-----|-------------|
| D1 | **Shipped harness not in morning prompts/capabilities on main** | Partial worktree only; main mornings can re-propose buildout |
| D2 | **Banned buildout IDs not enforced in schema** | Soft prompt law only |
| D3 | **No registry of shipped research CLIs** | Each new tool repeats awareness problem |

### E. Workflow engine / ops gaps

| ID | Gap | Why it hurt |
|----|-----|-------------|
| E1 | **Plan SCHEMA checks too weak for full ExecutionPlan** | Hermes “succeeded” with invalid pm-desk plan |
| E2 | **Resume UX** (`run --resume` needs path_or_id) | Easy to botch gate continue |
| E3 | **No automatic post-approve provision dry-run summary** when monitors=0 | Unclear what approve did |
| E4 | **Thesis reopen YAML lives only on host** (`pm-thesis-reopen-v0.yaml` dirty on `/opt`) | Not in PR process |

### F. Process / agent behavior gaps (meta)

| ID | Gap | Why it hurt |
|----|-----|-------------|
| F1 | **Lazy stop** culture in harness + operators | “Scrape broken → buildout” instead of find JSON |
| F2 | **Fixture used as production prior** | False investigate_long |
| F3 | **Approval asked for work Joe said should be free** | Coding/scraping ≠ Joe gate |

---

## Design principles (non-negotiable for implementer)

1. **Joe gate = spend / credentials / external infra / live size risk only.**  
   Examples that **auto-run**: public scrapes, loader fixes, fixture updates, `cpi-calibrate`, Browserbase on allowlisted specs, code in `optional-projects/pm-desk`.  
   Examples that **need Joe**: paid API keys, new vendor contracts, enabling Hermes webhooks that edit global config, live trading, non-paper wallet, standing always-on daemons with cloud cost above threshold.

2. **Never present fixture calibration as trade evidence.**  
   `CalibrationResult` must carry `series_provenance: fixture|live|mixed` and plan/entry rules: **fixture ⇒ max decision `research_only` / no monitors for size**.

3. **Buildout completion must be able to reopen the thesis** in the same workspace with prior context (productized `pm-thesis-reopen` or morning `--continue-from`).

4. **Edge-first Joe surfaces** (telegram_brief + buildout cards): claim → why gap can exist → measured numbers → kills → what approve does.

5. **Boil the ocean on public data:** ordered source ladder; fail only after ladder exhausted; log every attempt in workspace.

6. **Paper only** unless Joe later unlocks live.

---

## Target approval model

```
                    ┌─────────────────────────────┐
                    │  Agent needs something new  │
                    └──────────────┬──────────────┘
                                   ▼
              Is it public data / code / local scrape?
                     │ yes                    │ no
                     ▼                        ▼
            AUTO (no Joe)              Needs $ / key / vendor / live size?
            - implement loader                  │ yes
            - run calibrate                     ▼
            - BB allowlisted              JOE GATE (narrow)
            - write workspace             - buy datafeed
                                          - secrets
                                          - always-on paid infra
                                          - live execution
```

**Plan field change (conceptual):**

```ts
// proposed_buildouts[] item
approval_class: 'auto_agent' | 'joe_infra' | 'joe_live_risk'
// provisioner / operator:
// - auto_agent: may be executed by coding agent without Telegram
// - joe_*: stays on paper_gate / separate infra gate
```

Morning `paper_gate` should mean: **“approve monitor install + paper posture for this plan”**, not “approve eng backlog.”

---

## Proposed approach (phased PR — one CC Pro PR is fine if scoped)

### Phase 1 — Data plane truth (CPI + pattern)
- Fix Cleveland loader to prefer `nowcast_year.json` / `nowcast_month.json`.
- BLS prints from public API or Actual series in same JSON.
- Provenance on every calibrate result; refuse monitor packaging on fixture-only.
- Multi-attempt ladder + workspace `data_plane_log.json`.

### Phase 2 — Approval split + auto eng
- Schema + prompts: `approval_class`.
- Ban list for shipped tools (`cpi_nowcast_bucket_harness`).
- Doc + optional `pm-desk research doctor` listing shipped CLIs.

### Phase 3 — Reopen productized
- Ship `pm-thesis-reopen-v0` properly (from host experiment).
- Input: `prior_run_id`, focus market, workspace `pm-desk`.
- Force harness run + live ladder before plan.
- Plan must use library prompt `pm-execution-plan-v1` (strict), not freestyle.
- Edge-first telegram_brief checklist enforced in prompt + light validator.

### Phase 4 — Joe briefing + gate UX
- `plan render-telegram` sections: CLAIM / WHY GAP / MEASURED / KILLS / IF YOU APPROVE.
- Buildout render same structure.
- After gate approve: print “monitors installed: 0” / provision dry-run summary automatically (CLI or workflow notify).

### Phase 5 — Scout anti-laziness
- DD/DQ/directive prompts: before any `proposed_buildouts` for data, must show ladder attempts.
- Debate may fail a plan that cites fixture as entry edge.

---

## Step-by-step implementation tasks

### Task 1: Worktree + branch

**Objective:** Clean fork worktree from latest main.

```bash
cd /opt/hermes-agent && git fetch origin main
git worktree add -b feat/pm-desk-control-plane-hardening \
  /home/hermes/research/hermes-pm-desk-control-plane-hardening origin/main
```

---

### Task 2: CalibrationResult provenance (fail closed on fixture-as-edge)

**Files:**
- Modify: `optional-projects/pm-desk/src/research/cpi_nowcast/types.ts`
- Modify: `optional-projects/pm-desk/src/research/cpi_nowcast/calibrate.ts` / `index.ts` / `compare.ts`
- Modify: `optional-projects/pm-desk/src/cli/commands/research.ts`
- Test: `optional-projects/pm-desk/tests/cpi_nowcast.test.ts`

**Add fields (required):**
```ts
series_provenance: 'fixture' | 'live' | 'mixed'
source_urls: string[]
paired_n: number  // alias sample_size ok
entry_eligible: boolean  // false if fixture or mixed without joe override
```

**Rule:** `entry_eligible === false` when provenance is `fixture`.  
CLI must default fixture paths to `series_provenance: 'fixture'`.  
Live paths set `live` only if both nowcasts and prints came from live loaders.

**Tests:** fixture run ⇒ `entry_eligible false`, decision may still be `investigate_*` but labeled research-only; compare layer cannot emit “package monitors” flag.

**Commit:** `feat(pm-desk): calibration provenance and entry_eligible`

---

### Task 3: Boil-the-ocean Cleveland/BLS loaders

**Files:**
- Modify: `optional-projects/pm-desk/src/research/cpi_nowcast/loaders/http.ts`
- Optionally: `loaders/cleveland_year_json.ts`
- Test: unit tests with saved HTML/JSON fixtures under `fixtures/cpi_nowcast/live_samples/` (checked-in small slices, not 7MB full file — trim to 3–5 months + one full structure sample)

**Ladder for nowcasts (in order):**
1. `GET https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast_year.json?sc_lang=en`
2. Parse each chart: `subcaption` → ref_month; last numeric `CPI Inflation` → nowcast; last `Actual CPI Inflation` → print if present
3. Fallback: HTML table scrape for **current** month only (July row pattern already on page)
4. Fallback: MoM JSON only for MoM markets (not YoY) — document

**Ladder for prints if Actual missing:**
1. BLS public API `CUUR0000SA0` multi-year → compute YoY
2. Fail loud with attempts[] log

**Write** `data_plane_attempts` into result notes or sidecar.

**Commit:** `fix(pm-desk): Cleveland nowcast_year.json live ladder`

**Verify manually:**
```bash
PM_DESK_LIVE_CPI=1 npx tsx src/cli/pm-desk.ts research cpi-calibrate \
  --fetch-cleveland --fetch-bls \
  --as-of $(date -u +%F) --live-nowcast 3.42 --bucket 3.4 --mid 0.425 --json
```
Expected: `sample_size` ≫ 12, `series_provenance: live`, `decision: no_trade` (given current mid~0.42) or honest numbers.

---

### Task 4: Shipped-tool registry + banned buildout IDs

**Files:**
- Create: `optional-projects/pm-desk/src/research/registry.ts`
- Modify: `optional-projects/pm-desk/src/schema/execution-plan.ts` (zod refine)
- Modify: prompts `pm-execution-plan-v1`, `pm-dd-v1`, `pm-dq-v1`, `pm-directive-v1`, `pm-prepare-context-v1`
- Modify: `taxonomy/cards.yaml` nowcast card + `detectCapabilities()` include `cpi_nowcast_calibration` (if not already on main)
- Test: execution-plan rejects banned ids; taxonomy capability test

**Registry example:**
```ts
export const SHIPPED_RESEARCH_TOOLS = [
  {
    id: 'cpi_nowcast_bucket_harness',
    cli: 'pm-desk research cpi-calibrate',
    capability: 'cpi_nowcast_calibration',
    status: 'shipped',
  },
] as const
```

**Zod:** reject `proposed_buildouts[].id` in shipped set with clear error.

**Commit:** `feat(pm-desk): shipped research registry + ban re-propose`

---

### Task 5: Approval class on buildouts

**Files:**
- `src/schema/execution-plan.ts`
- `src/plan/render.ts`
- prompts plan/dd
- tests

**Add:**
```ts
approval_class: z.enum(['auto_agent', 'joe_infra', 'joe_live_risk']).default('joe_infra')
```

**Prompt law:**
- Scrapers, public APIs, in-repo code → agents should **do the work** or emit `auto_agent` only as a notebook for humans — prefer **just run**.
- `joe_infra`: paid feed, new secret, webhook enable, cloud daemon $
- `joe_live_risk`: anything implying live size / non-paper

**Render:** Joe Telegram shows **only** `joe_*` buildouts under “NEEDS YOUR OK”; `auto_agent` listed as “agent should execute (not a Joe blocker)”.

**Commit:** `feat(pm-desk): buildout approval_class joe vs auto`

---

### Task 6: Productize thesis reopen workflow

**Files:**
- Move/polish: `optional-projects/pm-desk/workflows/pm-thesis-reopen-v0.yaml` (from host experiment)
- Create prompt libs: `pm-reopen-context-v1.yaml`, `pm-reopen-research-v1.yaml` (or reuse dd/plan with params)
- Wire `hermes install-prompts`
- CLI helper: `pm-desk thesis reopen --prior-run <id> --focus-token <id> --json` writes `thesis_reopen_active.json` + prints workflow run command
- Tests: hermes workflow validate; optional fixture dry-run

**Graph (keep simple):**
```
context → research → plan → paper_gate
```

**Research node MUST:**
1. Read prior run dir via workspace
2. Run live calibrate ladder (not fixture-only unless live failed after full ladder — then fail_closed entry)
3. Snapshot market mid
4. Write `reopen_research.json` + `cpi_calibrate.json`
5. Set verdict using `entry_eligible`

**Plan node:** **must** use `library: pm-execution-plan-v1` (strict schema), not freestyle inline schema alone.

**Commit:** `feat(pm-desk): productize thesis reopen workflow`

---

### Task 7: Edge-first brief validator

**Files:**
- `src/plan/brief.ts` or extend render
- `src/schema/execution-plan.ts` optional refine on `telegram_brief`
- tests

**Require telegram_brief to contain labeled sections (case-insensitive):**
```
CLAIM
WHY GAP CAN EXIST
MEASURED
KILLS
IF YOU APPROVE
```

Soft fail in render (warn) + hard fail in `plan validate --strict-brief` flag default **on** for reopen, **warn** for morning initially.

**Commit:** `feat(pm-desk): edge-first telegram brief contract`

---

### Task 8: Gate continue + post-approve summary

**Files:**
- Docs in `optional-projects/pm-desk/README.md` or `src/research/README.md`
- Optional CLI: `pm-desk plan after-gate --run-id` prints monitors count + provision dry-run if monitors>0
- Fix operator notes for:  
  `hermes workflow run workflows/….yaml --resume <run_id>`

**Commit:** `docs(pm-desk): gate resume and post-approve summary`

---

### Task 9: Scout anti-laziness prompts

**Files:** prompts dd/dq/directive + debate inline if needed

**Add DATA PLANE LAW:**
- Before `proposed_buildouts` for missing data: list URLs attempted, status codes, next ladder step.
- Forbidden: “PDF unreachable” / “nowcast unavailable” after single curl.
- Forbidden: use fixture calibrate for `paper_plan_candidate` / non-empty monitors.

**Commit:** `fix(pm-desk): anti-laziness data-plane prompt law`

---

### Task 10: Integration verify + PR

```bash
export PATH="$HOME/.local/node-v24.18.1-linux-x64/bin:$PATH"
cd optional-projects/pm-desk
npm ci && npm run check

# live (budgeted)
PM_DESK_LIVE_CPI=1 npx tsx src/cli/pm-desk.ts research cpi-calibrate \
  --fetch-cleveland --as-of $(date -u +%F) --live-nowcast 3.42 --bucket 3.4 --mid 0.425 --json

hermes workflow validate workflows/pm-thesis-reopen-v0.yaml
hermes workflow validate workflows/pm-morning-generator-v0.yaml

# optional cheap reopen dry-run
```

**PR title:** `feat(pm-desk): control-plane hardening — live data ladder, approval classes, thesis reopen`

**PR body must include:** gap table A–F, Joe approval policy, live calibrate sample output, non-goals (no live trading, no paid feeds without gate).

---

## Files likely to change (summary)

| Path | Change |
|------|--------|
| `src/research/cpi_nowcast/**` | Live JSON ladder, provenance |
| `src/research/registry.ts` | Shipped tools |
| `src/schema/execution-plan.ts` | approval_class, bans, brief |
| `src/plan/render.ts` | Joe-facing sections |
| `src/cli/commands/research.ts` | provenance CLI |
| `src/cli/commands/thesis.ts` (new) | reopen packer |
| `workflows/pm-thesis-reopen-v0.yaml` | productize |
| `workflows/prompts/pm-*.yaml` | awareness + anti-lazy + edge-first |
| `taxonomy/cards.yaml` + `src/taxonomy/index.ts` | capability |
| `tests/**` | provenance, bans, brief, reopen validate |
| `README` / research README | operator |

---

## Tests / validation matrix

| Check | Command / signal |
|-------|------------------|
| Unit | `npm run check` |
| Live ladder | `PM_DESK_LIVE_CPI=1 … cpi-calibrate --fetch-cleveland` → N≥100, provenance live |
| Ban | plan with `cpi_nowcast_bucket_harness` → validate throws |
| Fixture | fixture calibrate → `entry_eligible false` |
| Workflow | `hermes workflow validate` morning + reopen |
| Brief | missing CLAIM section → strict-brief fail |
| Manual | reopen dry-run with prior_run_id input |

---

## Risks and tradeoffs

| Risk | Mitigation |
|------|------------|
| Cleveland JSON moves again | Ladder + checked-in sample fixtures + loud fail |
| Auto-scrape policy too broad | Allowlist domains; BB still double opt-in; no auth walls |
| Joe still flooded | Only `joe_*` buildouts in Telegram needs-OK |
| Strict brief too brittle | Start warn-on-morning / strict-on-reopen |
| Scope creep into daemons | Out of scope this PR — registry only |

---

## Open questions (Joe — defaults assumed if silent)

1. **Auto-agent spend cap:** OK to auto-run Browserbase on allowlisted specs during reopen without asking? **Default: yes if already configured; no new paid vendors.**
2. **Should morning `paper_gate` auto-skip when monitors=[] and only auto_agent buildouts?** **Default: still gate once for “record morning” but brief must say “nothing installs.”**
3. **Merge reopen into morning via flag vs separate workflow?** **Default: separate workflow + CLI packer (clearer).**

---

## Non-goals (this PR)

- Live trading / wallet
- Paid data subscriptions without joe_infra
- Always-on vol daemon production deploy
- Rewriting whole morning model roster
- Fixing unrelated Hermes core beyond optional plan SCHEMA tighten if cheap

---

## Success criteria

- [ ] Live `cpi-calibrate` works without HTML regex; provenance `live`
- [ ] Fixture can never justify non-empty monitors / entry_eligible
- [ ] Shipped harness cannot reappear in `proposed_buildouts`
- [ ] `approval_class` separates Joe infra from auto eng
- [ ] `pm-thesis-reopen-v0` validated + documented; prior-run context works
- [ ] Telegram brief edge-first sections enforced (strict on reopen)
- [ ] `npm run check` green; PR open; Joe saw this plan before CC Pro launch

---

## Suggested CC Pro launch note (after Joe OKs plan)

- Profile: **`claude-pro` only** (not glm/OR) for multi-commit PR
- Worktree path above; directive = this file
- No `--max-turns`; incremental commits per task
- Independent verify by Hermes after exit (don’t trust subtype alone)

---

## One-paragraph “why this plan”

We almost bought a fake edge because the system allowed **fixture math**, **lazy data failure**, and **Joe-gated coding** to substitute for **turning over public stones** and **looping research after tools exist**. Hardening is not more taxonomy cards — it is provenance, source ladders, approval classes, a real reopen path, and briefs that force CLAIM/MEASURED/KILLS before anyone clicks approve.
