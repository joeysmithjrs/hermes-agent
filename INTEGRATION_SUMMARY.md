# PM Desk — Hermes-fork integration summary

Branch `feat/pm-desk-mvp`, PR [#9](https://github.com/joeysmithjrs/hermes-agent/pull/9).
Everything below was run on this host; commands and their real output are
reproduced verbatim.

---

## 1. Where it lives

| | |
|---|---|
| **Final path** | `optional-projects/pm-desk/` |
| **Previously** | top-level `pm-desk/` |
| **npm workspace?** | **No.** Root `workspaces` is `["apps/*", "ui-tui", "ui-tui/packages/*", "web", "tests-js"]` — unchanged. |
| **Root dependency overlap** | none |
| **Root postinstall** | unchanged (`echo '✅ Browser tools ready. …'`) |
| **Core/tool-schema footprint** | none — no model tool, no toolset entry, no `tools/*.py`, no gateway platform |
| **Install / test** | `cd optional-projects/pm-desk && npm ci && npm run check` |

### Why here, and not somewhere else

Three placements were considered and rejected against real repo state:

- **`apps/pm-desk`** — `apps/*` is globbed into the root npm workspaces. Every
  Hermes contributor's `npm ci` would then build `better-sqlite3` natively and
  resolve `@polymarket/client`, which requires Node >= 24, inside a JS CI lane
  that pins Node 22 (`.github/workflows/js-tests.yml`).
- **`plugins/pm-desk`** — `AGENTS.md` (June-2026 policy) closes that tree to
  third-party-product and vendor-SaaS integrations. PM Desk integrates
  Polymarket and Browserbase.
- **`optional-skills/`** — that tree holds `SKILL.md` directories consumed by
  the skills index and the docs-site `site` lane. A 90-file TypeScript package
  there breaks both generators.

`optional-projects/` follows the existing `optional-skills/` / `optional-mcps/`
idiom — shipped in the repo, discoverable, off by default. The contract is
written down in [`optional-projects/README.md`](optional-projects/README.md) and
`AGENTS.md`'s project-structure tree points at it. Precedent for a non-workspace
in-tree Node package already exists at `scripts/whatsapp-bridge/`.

---

## 2. Carry-forward proof

**Correction to a premise in the directive:** the transplant commit `0ee45ceb7`
*already contained* the substance of standalone commit `266131d`. The transplant
copied the standalone working tree at a point after that fix, even though the
commit itself was never cherry-picked. Verified — the committed
`scripts/demo-offline.sh` in the original transplant already had `stop_server`
and the direct `node --import tsx` listener. Nothing had to be ported.

That is asserted mechanically rather than claimed:

```console
$ ./optional-projects/pm-desk/scripts/verify-carry-forward.sh
source : /home/hermes/pm-desk @ feat/pm-desk-mvp
fork   : /home/hermes/research/hermes-pm-desk-mvp @ optional-projects/pm-desk (HEAD)

OK: all 92 standalone tracked paths are present

INFO: added during Hermes integration (expected):
  .npmrc
  scripts/monitor-sweep.sh
  scripts/preflight.d.mts
  scripts/preflight.mjs
  scripts/verify-carry-forward.sh
  src/cli/commands/hermes.ts
  src/hermes/package-paths.ts
  src/hermes/prompts.ts
  tests/hermes.test.ts
  workflows/prompts/pm-dd-v1.yaml
  workflows/prompts/pm-directive-v1.yaml
  workflows/prompts/pm-dq-v1.yaml
  workflows/prompts/pm-eval-v1.yaml
  workflows/prompts/pm-prepare-context-v1.yaml

  CHANGED (intentional): .env.example — accurate launcher variable names and semantics
  CHANGED (intentional): README.md — Node 24, advisories, prompt install, workspaces, cron, CI
  CHANGED (intentional): eslint.config.js — eslint 10 type-checks scripts/*.mjs
  CHANGED (intentional): package-lock.json — follows package.json
  CHANGED (intentional): package.json — engines.node >=24, eslint 10, preflight + check scripts
  CHANGED (intentional): src/cli/pm-desk.ts — registers the `hermes` subcommand
  CHANGED (intentional): src/ingress/dispatcher.ts — absolute default workflow path + dryRun option
  CHANGED (intentional): workflows/pm-desk-paper-v0.yaml — documents what `workspace:` actually is
  CHANGED (intentional): workflows/pm-signal-adjudication-v0.yaml — drops unusable `workspace:` on a tools-empty node

OK: 83 file(s) byte-identical, 9 intentionally changed

OK: 266131d demo cleanup (stop_server / direct node listener) is present
$ echo $?
0
```

The script compares **git blob hashes of the two committed trees** — not
timestamps, not file counts. Every intentionally-changed file carries a recorded
reason; any *unrecorded* difference fails the check. It exits 0 and skips where
the standalone repo is absent, so it is a no-op on other checkouts and in CI.

The demo-cleanup fix was also exercised for the behaviour it exists to prevent —
a failure *after* the server starts:

```console
$ sed 's|^\$CLI ingress outbox --home "\$HOME_DIR"$|false|' scripts/demo-offline.sh > /tmp/demo-fail.sh
$ PM_DESK_DEMO_PORT=8799 bash /tmp/demo-fail.sh /tmp/pm-desk-demo-fail
EXIT=1 (expected non-zero)
=== listener on 8799 after failed demo ===
NO LEAKED LISTENER
=== stray processes ===
NONE
```

History is additive: seven commits on top of the published transplant. No
force-push, no rewrite of shared history. Authorship on the original 92 files is
preserved — the relocation is 92 renames at 100% similarity.

---

## 3. Hermes wiring — what is real

### 3.1 Both workflows validate against the installed engine

```console
$ hermes workflow validate optional-projects/pm-desk/workflows/pm-desk-paper-v0.yaml
OK  workflow=pm_desk_paper_v0 hash=sha256:1b620a76f7cf39e92fd2d6f05c66ccaff327905399eea10e4fc13683f1a9c1e6

$ hermes workflow validate optional-projects/pm-desk/workflows/pm-signal-adjudication-v0.yaml
OK  workflow=pm_signal_adjudication_v0 hash=sha256:c3f57a0565d8f9289be71818a969d6e4cbc18ae2d892c3c6db1edd1633914075
```

Dry run, against a throwaway `HERMES_HOME`, with the exact argv the launcher
builds — compiles and plans, spawns no agent, calls no model:

```console
$ HERMES_HOME=$(mktemp -d) hermes workflow run \
    optional-projects/pm-desk/workflows/pm-signal-adjudication-v0.yaml \
    --input '{"signal_id":"sig_x","prompt":"p","paper_only":true}' --dry-run
{
  "run_id": "wf_2a027aa95eb4",
  "workflow_id": "pm_signal_adjudication_v0",
  "status": "dry_run",
  "ready": ["adjudicate"],
  "node_runs": 1
}
```

### 3.2 The bug that mattered: prompt libraries existed on exactly one machine

`pm_desk_paper_v0` uses `spec.prompt: {library: <name>}` for five libraries that
were **not in the repository at all** — they lived only in
`/home/hermes/.hermes/workflows/prompts/`. Hermes hard-rejects an unknown
library (`PROMPT_LIBRARY`; `workflow/prompts/library.py` has no
not-found-means-empty fall-through), so the research spine validated on one host
and nowhere else. Proven both ways against a temp home:

```console
$ export HERMES_HOME=$(mktemp -d)
$ hermes workflow validate workflows/pm-desk-paper-v0.yaml
REJECTED
  [ERROR] PROMPT_LIBRARY prepare: unknown prompt library 'pm-prepare-context-v1'
          (searched: /tmp/tmp.FcYj3zSvlD/workflows/prompts,
                     /opt/hermes-agent/workflow/prompts/builtin). …
  [ERROR] PROMPT_LIBRARY directive_branch: unknown prompt library 'pm-directive-v1' …
  [ERROR] PROMPT_LIBRARY dq: unknown prompt library 'pm-dq-v1' …

$ npx tsx src/cli/pm-desk.ts hermes install-prompts --hermes-home "$HERMES_HOME"
DRY RUN — nothing written. Target: /tmp/tmp.FcYj3zSvlD/workflows/prompts

library                action   target
---------------------  -------  ------------------------------------------------
pm-dd-v1               install  …/workflows/prompts/pm-dd-v1.yaml
pm-directive-v1        install  …/workflows/prompts/pm-directive-v1.yaml
pm-dq-v1               install  …/workflows/prompts/pm-dq-v1.yaml
pm-eval-v1             install  …/workflows/prompts/pm-eval-v1.yaml
pm-prepare-context-v1  install  …/workflows/prompts/pm-prepare-context-v1.yaml

Re-run with --apply to install.

$ npx tsx src/cli/pm-desk.ts hermes install-prompts --hermes-home "$HERMES_HOME" --apply
Installed 5 prompt libraries into /tmp/tmp.FcYj3zSvlD/workflows/prompts

$ hermes workflow validate workflows/pm-desk-paper-v0.yaml
OK  workflow=pm_desk_paper_v0 hash=sha256:1b620a76…
```

The five libraries now ship at `optional-projects/pm-desk/workflows/prompts/`.
The installer is **dry-run by default** (`--apply` writes), refuses to clobber a
locally-edited library without `--force`, and every test targets an explicit
temp directory. The real `/home/hermes/.hermes` was not written to at any point
during this work.

### 3.3 Capability boundary — structurally asserted

`tests/hermes.test.ts` parses both workflow YAMLs and asserts:

- the adjudication workflow has exactly one agent node, `adjudicate`, whose
  `tools` is **present and empty** — not absent (which would inherit the parent
  agent's toolset) and not a deny-list (`spec.deny_tools` is not enforced by any
  Hermes execution path);
- no node in *either* workflow, including fanout branch specs, grants
  `terminal`, `browser_*`, `web_*`, `write_file`, `patch` or `delegate_task`;
- every agent node states a `tools` grant explicitly;
- every `library:` name a workflow references is shipped in this package.

The pre-existing `tests/guard.test.ts` (5 tests) still scans all of `src/` for
trading/signing/wallet symbols and asserts exactly one module imports
`@polymarket/client`, importing only `createPublicClient`.

### 3.4 Who writes the ledger — the honest boundary

The workflow **cannot** write PM Desk's SQLite ledger. Hermes resolves workflow
`run:` callables against a frozen allowlist (`best`, `concat`, `first_k`,
`judge_converge`, `majority`, `top_k`, `workflow.examples.echo`,
`workflow.examples.notify_telegram`), so a `pm_desk.record` script node would
not resolve. No fake callable was added. The real chain:

```
pm-desk (deterministic code)  →  renders the prompt, hands it over as --input
hermes workflow               →  produces a schema-validated adjudication artifact
pm-desk workflow adjudicate   →  re-validates, and ONLY a `paper_alert` becomes
                                 a paper ledger row
```

`pm-desk workflow adjudicate --result <file>` is that bridge and already
existed; it is exercised by the offline demo, where it correctly **refuses** to
create a ledger row because no market observation backs the signal:

```
LEDGER_INVARIANT_ERROR: slippage_rule cross_spread_full needs a market snapshot,
which this entry observation does not have
```

No LLM is involved in detection — the monitor engine is deterministic code.

### 3.5 Ingress → outbox → launcher

Unchanged and still correct: loopback-only HMAC ingress, record-**before**-
dispatch, `outbox` dispatcher by default (queues an artifact, invokes nothing).
The opt-in launcher runs `hermes workflow run <path> --input '<json>'` through
`execFile` (argv array, never a shell string; there is no `--input-file` flag).

Two fixes:

- The launcher defaulted to the **cwd-relative** path
  `workflows/pm-signal-adjudication-v0.yaml`, which only resolved when
  `pm-desk ingress serve` happened to be run from the package root. It now
  defaults to the packaged **absolute** path.
- A new `dryRun` option (`PM_DESK_HERMES_DRY_RUN=1`) appends `--dry-run`, so an
  operator can prove the wiring at zero cost.

Coverage: 24 tests in `tests/ingress.test.ts` (argv shape, catalog mode,
failure-becomes-`failed`-outbox-row, record-survives-dispatcher-throw), plus the
launcher tests in `tests/hermes.test.ts`, plus three **opt-in tests that drive
the real `hermes` binary** against a temp `HERMES_HOME`:

```console
$ PM_DESK_HERMES_INTEGRATION=1 npm test
 ✓ tests/hermes.test.ts (18 tests) 3583ms
   ✓ real Hermes CLI (opt-in) > validates the adjudication workflow with no prompt libraries installed  851ms
   ✓ real Hermes CLI (opt-in) > rejects the research spine until its prompt libraries are installed, then accepts it  1760ms
   ✓ real Hermes CLI (opt-in) > accepts the exact argv the launcher builds (compile + plan only, no agent)  794ms

 Test Files  12 passed (12)
      Tests  216 passed (216)
```

They are opt-in because Hermes is a Python install this package's Node-only CI
does not provision.

Global webhook configuration was **not touched**. `hermes webhook` commands
remain documented-only in the README.

### 3.6 Workspaces — what the primitive actually is

A Hermes workspace is a named persistent directory at
`$HERMES_HOME/workflows/workspaces/<name>/` that agent nodes read and write with
their **ordinary file tools** (`workflow/store/workspace.py`). Two things it is
not:

- **not a bind of the PM Desk directory.** There is no supported primitive for
  pointing a workflow at an arbitrary external path. This is a documented
  limitation, not a workaround.
- **not a per-node isolation boundary.** `spec.workspace` and `spec.profile` are
  in `ir.OVERRIDE_ONLY_FIELDS`; `workflow/verify.py` rejects them outright
  because no execution path enforces them. No such guarantee was added.

Acting on that: `pm_signal_adjudication_v0` **drops** its `workspace: pm-desk`
line — a `tools: []` node has no file tools and could never open a workspace, so
pinning one would create an empty directory in the operator's Hermes home and
imply a binding that does not exist. `pm_desk_paper_v0` keeps it (its nodes hold
`read_file`) with a comment saying exactly what it is.

### 3.7 Scheduling — opt-in, script-first, no agent

`optional-projects/pm-desk/scripts/monitor-sweep.sh` is the scheduled unit.
Detection is deterministic, so it is a script and not an agent turn. It prints
nothing when nothing fired, which pairs with `hermes cron create --no-agent`
(contract: "empty stdout = silent") to make a quiet desk cost zero tokens and
send zero notifications. Verified both ways:

```console
$ PM_DESK_DIR="$PWD" PM_DESK_HOME=/tmp/pm-desk-sweep-test ./scripts/monitor-sweep.sh
(no output)
EXIT=0

# after collecting fixture v1 then the changed v2:
$ PM_DESK_DIR="$PWD" PM_DESK_HOME=/tmp/pm-desk-sweep-test ./scripts/monitor-sweep.sh
PM DESK — 1 monitor signal(s) fired (PAPER ONLY, no order can result)
local ingress not running on 127.0.0.1:8787 — signals are recorded in the store; …
{ "version": 1, "signal_id": "sig_0b1320e964c4d5c84507b6418de0479f", … }
EXIT=0
```

**No cron job was created.** The `hermes cron create` recipe is documented for
the operator to run.

---

## 4. Node version — resolved as option 1 (hard prerequisite)

`@polymarket/client` declares `engines.node: ">=24"` in **every** published
version. Checked directly:

```console
$ for v in 0.1.0 0.2.0 0.3.0-beta.0 0.1.0-beta.18; do npm view @polymarket/client@$v engines --json; done
{"node":">=24"}
{"node":">=24"}
{"node":">=24"}
{"node":">=24"}
```

There is therefore **no older release to pin** — option 2 is unavailable. The
package now declares `>=24.0.0`, matching its hardest dependency:

```console
$ npm install                    # Node 22.23.1
npm error code EBADENGINE
npm error notsup Required: {"node":">=24.0.0"}
npm error notsup Actual:   {"npm":"10.9.8","node":"v22.23.1"}

$ node scripts/preflight.mjs     # Node 22.23.1
PM Desk requires Node >= 24. This is Node 22.23.1.

Reason: @polymarket/client declares engines.node ">=24" in every published
version, so there is no compatible older release to pin. …
EXIT=1
```

`.npmrc` sets `engine-strict=true` so npm refuses rather than warning, and
`scripts/preflight.mjs` covers checkouts installed elsewhere. **The claim that
the suite "passes on Node 22" is not a support claim** — the offline suite
exercises the SDK through a fake and never loads it.

Node 24.18.1 was installed locally (`/home/hermes/.local/node24`) purely to
verify on the declared version; nothing in the repo references it.

---

## 5. Security advisories — resolved, and the discrepancy explained

Both earlier readings were correct and described different scopes. All five
high-severity advisories were **one root cause, entirely dev-only**:

```
GHSA-mh99-v99m-4gvg — brace-expansion <= 5.0.7, DoS via unbounded expansion
CVSS 7.5 (CWE-400, CWE-770)

reached only through:  eslint@9 → @eslint/eslintrc, @eslint/config-array
                              → minimatch → brace-expansion
```

Zero production dependencies were ever affected, which is why `--omit=dev`
reported 0 while a plain `npm audit` reported 5.

Fixed by **understanding the upgrade, not by `audit fix --force`**: `eslint@^10.8.0`
(engines `^20.19.0 || ^22.13.0 || >=24` — imposes no new Node floor) with
`@eslint/js@^10.0.1` and `typescript-eslint@^8.65.0`, whose peer range is
`eslint: ^8.57.0 || ^9.0.0 || ^10.0.0`. Current state:

```console
$ npm audit --omit=dev --audit-level=low
found 0 vulnerabilities
$ npm audit --audit-level=high
found 0 vulnerabilities
```

CI runs both scopes separately so a dev-tool advisory can never again be
mistaken for a shipped one.

---

## 6. CI

PR #9's `JS & TS checks` were skipped because that lane discovers packages via
`npm query .workspace` and PM Desk is deliberately not a workspace. Fixed with a
dedicated lane rather than by making it a workspace.

| | |
|---|---|
| **Workflow** | `.github/workflows/pm-desk.yml`, job name `optional-projects/pm-desk / check` |
| **Trigger** | new `pm_desk` classifier lane — any path under `optional-projects/pm-desk/` |
| **Node** | 24 (the JS & TS matrix stays on 22, unaffected) |
| **Steps** | `npm ci` → preflight → lint → typecheck → test → build → `npm audit --omit=dev --audit-level=low` → `npm audit --audit-level=high` → offline demo → no-leaked-listener assertion → no-trackable-runtime-state assertion |
| **Gate** | added to `all-checks-pass`, so branch protection still needs one check |

`npm ci` is used **without** `--ignore-scripts`: `better-sqlite3` needs its
install script for the native binding, and suppressing it is exactly why an
earlier verification attempt failed. That contract is now stated in the README
and enforced by CI actually running the suite.

The classifier also stops running the Python suite for `optional-projects/`-only
changes — nothing in the Hermes tree imports that directory, so a full
pytest + Desktop E2E + Docker run on a TypeScript-only edit was pure waste.

```console
$ python -m pytest tests/ci/test_classify_changes.py -q
38 passed in 0.67s

$ /tmp/actionlint          # 1.7.7, whole workflows tree
$ echo $?
0
```

Note: **this** PR touches `.github/`, so the classifier fails open and every
lane runs. A subsequent PM-Desk-only change selects `pm_desk` alone; that is
what the five new classifier cases assert.

---

## 7. Verification — commands and results

All run from `optional-projects/pm-desk/` on Node 24.18.1 unless noted.

| Check | Command | Result |
|---|---|---|
| Clean install | `npm ci` | `added 258 packages … found 0 vulnerabilities` |
| Preflight | `npm run preflight` | `preflight OK — Node 24.18.1 satisfies engines.node >=24.0.0` |
| Full gate | `npm run check` | lint ✓, typecheck ✓, **213 passed \| 3 skipped (216)**, build ✓ |
| With real Hermes | `PM_DESK_HERMES_INTEGRATION=1 npm test` | **216 passed (216)**, 12 files |
| Guard test | (in suite) `tests/guard.test.ts` | 5 passed |
| Prod audit | `npm audit --omit=dev --audit-level=low` | `found 0 vulnerabilities` |
| Full audit | `npm audit --audit-level=high` | `found 0 vulnerabilities` |
| Offline demo | `./scripts/demo-offline.sh /tmp/pm-desk-demo-verify` | completed, exit 0 |
| Demo leaves nothing | `ss -ltn \| grep 8788`; `ps -eo pid,args \| grep '[p]m-desk\.ts'` | `NONE` / `NONE` |
| Demo cleanup on failure | injected failure after server start, port 8799 | `NO LEAKED LISTENER`, `NONE` |
| Workflow validate ×2 | `hermes workflow validate …` | both `OK` |
| Workflow dry run | `hermes workflow run … --dry-run` (temp `HERMES_HOME`) | `status: dry_run`, `ready: [adjudicate]` |
| Prompt install round trip | temp `HERMES_HOME`: reject → install → OK | as shown in §3.2 |
| Carry-forward proof | `./scripts/verify-carry-forward.sh` | exit 0; 83 identical, 9 recorded |
| CI classifier | `python -m pytest tests/ci/test_classify_changes.py -q` | 38 passed |
| CI workflow lint | `actionlint` (1.7.7) | exit 0 |
| Tracked runtime/secrets | `git ls-files` grep for `node_modules\|dist\|data\|logs\|.sqlite\|.env` | `NONE` |
| Ignore coverage | `git check-ignore` on `node_modules`, `dist`, `data`, `logs/x.log`, `coverage/x.json`, `.env`, `.env.local`, `data/desk.sqlite` | all ignored |

The only tracked `.env*` file is `.env.example`; every credential name in it has
an empty value.

**Not run, deliberately:** live Browserbase collection, live Polymarket smoke
tests, any live-LLM workflow run, `hermes webhook` mutations, `hermes cron
create`. No trade of any kind is possible from this code.

---

## 8. Commits

Additive on top of the published transplant; no force-push, no history rewrite.

```
0b33116d3 test(pm-desk): distinguish intentional integration edits from drift
a4749be06 docs(pm-desk): state the real Hermes surface, and add a no-agent cron recipe
05b2a6555 ci: give PM Desk its own path-scoped lane on Node 24
ea1d831a5 feat(pm-desk): ship the prompt libraries and an opt-in Hermes installer
5e97e15d9 fix(pm-desk): require Node 24 honestly and clear the dev-only advisories
b27486dad test(pm-desk): prove the standalone tree carried forward intact
44e9010f7 refactor(pm-desk): relocate under optional-projects/ as an edge-owned package
0ee45ceb7 feat(pm-desk): add paper-only prediction market desk MVP   ← the transplant
```

---

## 9. Intentional limits and open items

1. **The adjudication workflow has never reached a live model.** It validates
   and dry-runs; its output contract is exercised offline against a fixture.
   Decision quality is unmeasured.
2. **Live Browserbase collection has never run.** Fully unit-tested behind a
   fake browser and double-gated (`--live` plus an explicit confirmation flag),
   but a first live run may surface selector-timing issues no fixture predicts.
3. **A Hermes workspace cannot bind this package's directory.** No such
   primitive exists; documented rather than faked.
4. **PM Desk's ledger is not writable from a workflow.** The `run:` allowlist is
   frozen; the bridge is a separate deterministic CLI step by design.
5. **Node 24 only.** Not a policy preference — the SDK admits nothing older.
   Hermes' own root project stays on `>=20` and is unaffected.
6. **Not ready for deployment.** No operator alerting on desk failure, no
   artifact-store retention policy, no paper-vs-live divergence measurement.
   Run it as a research instrument.
7. **`Review label gate` fails, and that is correct.** It is triggered by
   `optional-projects/pm-desk/eslint.config.js`: `scripts/ci/classify_changes.py`
   treats **any** file whose basename starts with `eslint.config.` as
   CI-sensitive, because an eslint config can define fix functions that execute
   arbitrary code on the autofix runner. The PR also adds workflow files, which
   trigger it independently. The gate wants the `ci-reviewed` label, which
   asserts *a human maintainer reviewed those files*. No such review has
   happened, so the label was **not** applied — applying it would misrepresent
   review status to silence a gate that is doing its job. It needs a human
   reviewer's decision, not an agent's.
