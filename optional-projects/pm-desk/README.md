# PM Desk

A **local-first, paper-only, artifact-driven** prediction-market research and
alerting desk.

> Lives at `optional-projects/pm-desk/` in the Hermes fork. It is **not** an npm
> workspace and nothing in Hermes builds, installs, imports or runs it — see
> [`../README.md`](../README.md) for that contract. Requires **Node >= 24**.

Every morning it generates research directives from a deterministic seed, does
real tool-using due diligence on the survivors, and emits **one ExecutionPlan**:
an edge story, the monitors that would test it, and the exact commands that
would install them. Joe approves or denies that plan in Telegram. On approval a
deterministic provisioner installs the monitors as Hermes cron jobs, and from
then on pure code watches primary sources and public market data and stays
silent until something fires.

> **PAPER ONLY.** This desk cannot trade, sign, connect a wallet, place an order,
> or participate in UMA. That is enforced structurally — by an empty tool
> allowlist, by database CHECK constraints, by zod literals in the plan schema,
> and by a guard test that scans all of `src/` — not by convention. See
> [Paper-only boundary](#paper-only-boundary).

---

## The loop

```text
  cron (or by hand)
        │
        ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  workflows/pm-morning-generator-v0.yaml         ← run THIS one       │
  │                                                                      │
  │  prepare → seed → directive → dq → dd → eval → plan → paper_gate    │
  │   ↑         ↑        ↑        ↑     ↑      ↑      ↑                  │
  │  desk    compiled  fanout   default │   default  emits an            │
  │  state   directive  ≤ 4     reject  │   reject   ExecutionPlan       │
  │          cards              first   │                               │
  │                                     └─ the ONLY node with research   │
  │                                        tools: web + browser + read   │
  └──────────────────────────────────┬───────────────────────────────────┘
                                     │  ExecutionPlan (zod-validated)
                                     ▼
                        ┌────────────────────────────┐
                        │  TELEGRAM GATE             │
                        │  dual control · Joe        │
                        │  approve / shelve / modify │
                        └─────────────┬──────────────┘
                                      │ approve, recorded by Hermes
                                      ▼
  ┌──────────────────────────────────────────────────────────────────────┐
  │  DETERMINISTIC PROVISIONER   pm-desk provision apply                 │
  │  no agent · no model · no string from the plan is ever executed      │
  │                                                                      │
  │  writes MonitorSpecs + SourceSpecs + a per-schedule sweep script     │
  │  spawns `hermes cron create --no-agent` (argv array, via execFile)   │
  │  records every file and job in SQLite, so revoke is precise          │
  └──────────────────────────────────┬───────────────────────────────────┘
                                     ▼
                          monitors run in the background
                          silent unless something fires
                                     │
                                     ▼
                    notify · queue to ingress · paper ledger
```

Four properties hold this together:

1. **Agents never own a polling loop and never get tools to discover change
   after the plan is approved.** Collectors and monitors are deterministic code.
   By the time a model is invoked on a signal, the change is already detected
   and its evidence already hashed and stored.
2. **The approval covers the argv.** `pm-desk plan render-telegram` prints every
   setup command verbatim, and drops brief text rather than commands when the
   message would overflow. Dual control over a summary nobody could check is a
   rubber stamp.
3. **No string an agent wrote is executed.** The provisioner derives its own
   argv from the plan's validated monitors and spawns that. A plan's
   `apply_command` is display text; when it disagrees with the derived command,
   that is reported as drift and the derived one still runs.
4. **Nothing can flip a plan to approved except Hermes.** `pm-desk plan approve`
   reads the decision Hermes wrote to
   `runs/<id>/gate_signals/<gate>.json` when Joe answered Telegram. It never
   writes that file, and `modify` maps to denied.

Jump to [First live morning run](#first-live-morning-run) for the exact
commands.

---

## Architecture

The detection half — what a provisioned monitor actually does when it runs.

```text
┌──────────────────────────────────────────────────────────────────────────┐
│  DETERMINISTIC DATA PLANE  —  no LLM anywhere in this band               │
└──────────────────────────────────────────────────────────────────────────┘

  taxonomy/cards.yaml                    specs/sources/*.yaml
  (versioned directive cards)            (declared URL + selectors + hash rule)
          │                                        │
          ▼                                        ▼
  ┌───────────────────┐                   ┌────────────────────────┐
  │ seed compiler     │                   │ Browserbase collector  │
  │ src/taxonomy      │                   │ src/browserbase        │
  │                   │                   │  policy → fetch →      │
  │ sha256(date |     │                   │  declared extract →    │
  │  desk state |     │                   │  normalize → hash      │
  │  nonce)           │                   └───────────┬────────────┘
  └─────────┬─────────┘                               │
            │                 ┌─────────────────────┐ │
            │                 │ Polymarket public   │ │
            │                 │ SDK adapter         │ │
            │                 │ src/polymarket      │ │
            │                 │ @polymarket/client  │ │
            │                 │ createPublicClient  │ │
            │                 │ ONLY (read-only)    │ │
            │                 └──────────┬──────────┘ │
            │                            │            │
            ▼                            ▼            ▼
      ┌─────────────────────────────────────────────────────────────┐
      │  LOCAL EVIDENCE STORE   src/store   (SQLite + CAS)          │
      │                                                             │
      │  markets · outcome_tokens · market_observations             │
      │  source_snapshots · artifacts (sha256-addressed)            │
      │  monitor_state · monitor_decisions · signals                │
      │  signal_outbox · adjudications · paper_ledger               │
      └───────────────────────────┬─────────────────────────────────┘
                                  │
                                  ▼
                   ┌──────────────────────────────┐
                   │  MONITOR ENGINE  src/monitor │
                   │  declarative rule specs      │
                   │                              │
                   │  predicate → dedupe →        │
                   │  cooldown → emit             │
                   └──────────┬───────────────────┘
                              │
                   SILENT ────┤ (the common case; nothing happens)
                              │
                              ▼  exactly one schema-valid SignalEnvelope
                   ┌──────────────────────────────┐
                   │  LOCAL INGRESS  src/ingress  │
                   │  127.0.0.1 · HMAC-SHA256     │
                   │  record FIRST, then dispatch │
                   │  idempotent on dispatch      │
                   └──────────┬───────────────────┘
                              │
                 ┌────────────┴─────────────┐
                 ▼                          ▼
        ┌─────────────────┐        ┌──────────────────────┐
        │ outbox          │        │ hermes launcher      │
        │ (DEFAULT)       │        │ (OPT-IN, off)        │
        │ queues an       │        │ renders prompt, runs │
        │ artifact.       │        │ the workflow         │
        │ Invokes nothing.│        └──────────┬───────────┘
        └─────────────────┘                   │
┌──────────────────────────────────────────────────────────────────────────┐
│  JUDGEMENT BAND  —  the only LLM in the system                          │
└──────────────────────────────────────────────────────────────────────────┘
                                              │
                                              ▼
                              ┌───────────────────────────────┐
                              │ pm_signal_adjudication_v0     │
                              │ ONE agent node, `tools: []`   │
                              │ no terminal/browser/network   │
                              │                               │
                              │ ignore · watch · paper_alert  │
                              └───────────────┬───────────────┘
                                              │ paper_alert only
                                              ▼
                              ┌───────────────────────────────┐
                              │ PAPER LEDGER  src/ledger      │
                              │ paper_only=1 (CHECK)          │
                              │ fill_type=SIMULATED_NO_FILL   │
                              │ (CHECK)                       │
                              └───────────────┬───────────────┘
                                              ▼
                                   Telegram-ready text alert
                                   "PAPER ONLY" on line 1
```

**Every LLM claim in this band is traceable.** The adjudicator has `tools: []`,
so it cannot look anything up. Anything it asserts must come from the rendered
evidence, which is content-addressed and re-verifiable.

The generator's DD node *does* have research tools — that is the whole point of
having a separate generation phase. Deciding what the desk should watch requires
going and reading primary sources. Deciding whether one already-detected change
matters does not, and must not.

---

## Layout

```text
src/core         typed errors, UTC time, content hashing, deterministic ids
src/schema       zod schemas: SignalEnvelope, SourceSpec, MonitorSpec,
                 Adjudication, ExecutionPlan
src/plan         ExecutionPlan rendering, recovery from a Hermes run, gate-derived approval
src/provision    pure derivation → files + argv, then apply/revoke/status + records
src/store        SQLite migrations, repositories, content-addressed artifacts
src/polymarket   official public SDK adapter, normalizer, snapshots, polling feed
src/browserbase  navigation policy, deterministic extraction, live/fixture browsers
src/monitor      declarative rules, diffing, dedupe/cooldown engine
src/ingress      HMAC auth, local HTTP server, dispatchers
src/ledger       paper ledger, invariants, Telegram renderer
src/taxonomy     versioned directive cards + deterministic seed compiler
src/workflow     adjudication prompt rendering
src/hermes       packaged-asset paths + the opt-in prompt-library installer
src/cli          pm-desk command-line entry point
specs/           sample SourceSpecs and MonitorSpecs
fixtures/        offline HTML + adjudication fixtures (make the E2E runnable)
fixtures/plans/  a complete example ExecutionPlan, validated by the tests
taxonomy/        cards.yaml — the versioned directive taxonomy
workflows/       Hermes workflow definitions
workflows/prompts/  prompt libraries the generator needs (install them)
scripts/         offline demo, Node preflight, monitor sweep, carry-forward proof
tests/           unit + guard + end-to-end; tests/live/ is opt-in only
```

---

## Setup

**Node >= 24 is a hard requirement, not a preference.** `@polymarket/client`
declares `engines.node: ">=24"` in *every* published version (checked across
`0.1.0-beta.*`, `0.1.0`, `0.2.0`, `0.3.0-beta.0`), so there is no older release
to pin. `.npmrc` sets `engine-strict=true`, so an older Node fails the install
outright rather than warning and continuing:

```console
$ npm install          # on Node 22
npm error code EBADENGINE
npm error notsup Required: {"node":">=24.0.0"}

$ npm run preflight    # on Node 22
PM Desk requires Node >= 24. This is Node 22.23.1.
```

The offline test suite happens to pass on Node 22 because it exercises the SDK
through a fake and never loads it. That is not support and this package does not
claim it.

```bash
cd optional-projects/pm-desk
npm ci                    # NOT --ignore-scripts: better-sqlite3 needs its
                          # install script for the native binding
cp .env.example .env      # NAMES only — fill in locally, never commit
npx tsx src/cli/pm-desk.ts store init
```

`.env.example` documents variable names and nothing else. No secret value
appears anywhere in this repository, and `.gitignore` excludes `.env`, the
database, artifacts and logs.

The desk stores everything under `$PM_DESK_HOME` (default `./data`). Every
command takes `--home <dir>` to override it.

### Test, lint, typecheck, build

```bash
npm run check      # preflight + lint + typecheck + test + build (what CI runs)

npm test           # full offline suite; never touches the network
npm run lint       # eslint (typed rules)
npm run typecheck  # tsc --noEmit, strict
npm run build      # tsc -> dist/
npm run verify     # lint + typecheck + test

npm run test:live  # OPT-IN only; see "Live checks" below

# Opt-in: also run the three tests that drive the real `hermes` binary against
# a throwaway HERMES_HOME. Skipped by default — Hermes is a Python install that
# this package's Node-only CI does not provision.
PM_DESK_HERMES_INTEGRATION=1 npm test
```

CI runs `npm run check` plus both audit scopes and the offline demo on Node 24,
in its own path-scoped lane (`.github/workflows/pm-desk.yml`). It triggers only
when files under `optional-projects/pm-desk/` change, and installs nothing on
behalf of the root Hermes project.

### Dependency advisories

`npm audit` and `npm audit --omit=dev` both report **0 vulnerabilities**. This
was not always true: five high-severity advisories were all one root cause —
[GHSA-mh99-v99m-4gvg](https://github.com/advisories/GHSA-mh99-v99m-4gvg)
(`brace-expansion <= 5.0.7`) reached through `eslint@9 -> minimatch ->
brace-expansion`, i.e. entirely dev-only. Resolved by upgrading to `eslint@^10`,
not by `audit fix --force`. CI fails on any production advisory at `low` or
above, so the two scopes can never be confused again.

---

## Try it: the whole loop, offline

```bash
./scripts/demo-offline.sh
```

Runs store init → taxonomy compile → source collect (v1, then a changed v2) →
monitor evaluate → ingress serve + submit → prompt render → adjudicate → paper
ledger, entirely from checked-in fixtures. No network, no credentials, no LLM.

The same loop is asserted end-to-end in `tests/e2e.test.ts`.

---

## Commands

### Directive taxonomy

```bash
# Deterministic stratified seed: same date + desk state + nonce -> same cards.
npx tsx src/cli/pm-desk.ts taxonomy compile --max-cards 4
npx tsx src/cli/pm-desk.ts taxonomy compile --max-cards 4 --nonce retry-1 --json
npx tsx src/cli/pm-desk.ts taxonomy list
```

### Market discovery (official public SDK, read-only)

```bash
npx tsx src/cli/pm-desk.ts market discover --limit 5
npx tsx src/cli/pm-desk.ts market discover --limit 5 --dry-run --json
npx tsx src/cli/pm-desk.ts market list
npx tsx src/cli/pm-desk.ts market capability

# Snapshot a token (repeat --token, or use --market <id>)
npx tsx src/cli/pm-desk.ts market snapshot --token <token_id>
```

`--limit` is enforced against both the accumulated count and the page size, so
`--limit 5` never pulls 500 records from a public endpoint.

### Primary-source collection

```bash
# Validate a spec — touches no network at all.
npx tsx src/cli/pm-desk.ts source validate \
  --spec specs/sources/example_official_release.yaml

# Collect from a local fixture (the default, offline mode).
npx tsx src/cli/pm-desk.ts source collect \
  --spec specs/sources/example_official_release.yaml \
  --fixture fixtures/sources/example_official_release.v1.html

npx tsx src/cli/pm-desk.ts source history --id example_official_release
npx tsx src/cli/pm-desk.ts source list
```

### Monitor evaluation (no LLM in this path)

```bash
npx tsx src/cli/pm-desk.ts monitor list --dir specs/monitors
npx tsx src/cli/pm-desk.ts monitor evaluate \
  --spec specs/monitors/example_source_change.yaml

# Machine mode: prints ONLY a schema-valid envelope, ready to pipe.
npx tsx src/cli/pm-desk.ts monitor evaluate \
  --spec specs/monitors/example_source_change.yaml --json > signal.json

npx tsx src/cli/pm-desk.ts monitor decisions      # audit of every evaluation
```

Prints `SILENT` (plus a per-rule reason) when nothing fired. `--dry-run`
evaluates without writing state, and is byte-for-byte repeatable.

### Ingress

```bash
export PM_DESK_INGRESS_SECRET=$(node -e "console.log(require('crypto').randomBytes(32).toString('hex'))")

npx tsx src/cli/pm-desk.ts ingress serve            # binds 127.0.0.1:8787
npx tsx src/cli/pm-desk.ts ingress submit --file signal.json
npx tsx src/cli/pm-desk.ts ingress outbox
curl -s http://127.0.0.1:8787/health
```

Requests are signed `HMAC-SHA256("<timestamp>.<body>")` with headers
`x-pm-desk-timestamp` and `x-pm-desk-signature: sha256=<hex>`, inside a 300s
replay window.

| Response | Meaning |
|---|---|
| `202 accepted` | recorded and dispatched |
| `202 accepted` + `dispatch_error` | recorded; downstream failed, nothing lost |
| `200 duplicate` | already recorded *and* already dispatched |
| `400` | not JSON, or fails the SignalEnvelope schema |
| `401` | missing/bad signature, or outside the replay window |
| `413` | body over 1 MB |

### Workflow (prompt render / record a result)

```bash
npx tsx src/cli/pm-desk.ts workflow render --signal <signal_id>
npx tsx src/cli/pm-desk.ts workflow adjudicate --result adjudication.json

hermes workflow validate workflows/pm-signal-adjudication-v0.yaml
```

`workflow render` builds the prompt locally and calls no model.

### Paper ledger

```bash
npx tsx src/cli/pm-desk.ts ledger record --adjudication adjudication.json

npx tsx src/cli/pm-desk.ts ledger record --manual \
  --thesis "..." --outcome YES --size 100 --slippage mid_no_slippage --mid 0.32 \
  --expiry-s 86400 --markout-s 300,3600 --invalidation "..." \
  --i-am-recording-a-paper-entry-manually

npx tsx src/cli/pm-desk.ts ledger list
npx tsx src/cli/pm-desk.ts ledger show --entry <entry_id>
npx tsx src/cli/pm-desk.ts ledger annotate --entry <id> --kind markout --note "5m"
npx tsx src/cli/pm-desk.ts ledger render --entry <entry_id>
npx tsx src/cli/pm-desk.ts ledger export > paper-ledger.json
```

### Execution plan

```bash
npx tsx src/cli/pm-desk.ts plan validate --file plan.json
npx tsx src/cli/pm-desk.ts plan show --file plan.json
npx tsx src/cli/pm-desk.ts plan render-telegram --file plan.json
npx tsx src/cli/pm-desk.ts plan schema > execution-plan.schema.json

# Recover the plan a generator run produced, and stamp Hermes' gate decision.
npx tsx src/cli/pm-desk.ts plan from-run --run-id <id> --out plan.json
npx tsx src/cli/pm-desk.ts plan approve --file plan.json --run-id <id>
```

Everything except `from-run --out` and `approve` is read-only. There is a
complete example at `fixtures/plans/example_execution_plan.json`, which the test
suite validates so it cannot drift from the schema.

### Provision

```bash
# Preview. Writes nothing, spawns nothing. Works on a still-pending plan.
npx tsx src/cli/pm-desk.ts provision dry-run \
  --plan plan.json --hermes-home "$HERMES_HOME" --desk-home "$PM_DESK_HOME"

# Install. Needs approval.decision == approved AND the acknowledgement.
npx tsx src/cli/pm-desk.ts provision apply \
  --plan plan.json --hermes-home "$HERMES_HOME" --desk-home "$PM_DESK_HOME" \
  --i-approved-this-plan

npx tsx src/cli/pm-desk.ts provision status --plan-id <plan_id> --desk-home "$PM_DESK_HOME"
npx tsx src/cli/pm-desk.ts provision revoke --plan-id <plan_id> --desk-home "$PM_DESK_HOME"
```

`apply` is idempotent on `(plan_id, idempotency_key)`: running it twice spawns
nothing the second time. A previously *failed* action is retried, because "it
failed" and "it is done" must not look the same.

### Store

```bash
npx tsx src/cli/pm-desk.ts store status
npx tsx src/cli/pm-desk.ts store export --table signals --limit 20
```

---

## Browserbase (opt-in)

Browserbase is used for **deterministic collection of named primary sources**,
never for free-form browsing and never for Polymarket.

A `SourceSpec` declares everything up front:

```yaml
id: example_official_release
url: https://example.gov/release
allowed_domains: [example.gov]
wait_for: "main"
extract:
  text_selector: "main"
  fields:
    title: "h1"
    published_at: "time"
fingerprint: normalized_text_sha256
```

### What the collector does

- validates HTTPS, the domain allowlist, and the route **before** opening a
  session, then re-validates the URL it actually landed on (so a redirect off
  the allowlist stores nothing);
- runs exactly the declared selectors — no selector is ever generated, inferred
  or repaired at runtime;
- normalizes deterministically, hashes, and stores both the raw page and the
  canonical text as content-addressed artifacts;
- performs one navigation, one optional wait, one read, then tears the session
  down.

### What it refuses

- non-HTTPS URLs, URLs with embedded credentials, and any domain not on the
  spec's own allowlist (true subdomain matching — `example.gov.evil.com` fails);
- wallet, login/auth, account, checkout/payment, trading, order, position,
  deposit/withdraw and KYC routes;
- `polymarket.com` and its API hosts outright — market data comes from the
  official SDK, never from a browser;
- clicking, typing, form submission, uploads and downloads;
- persisting cookies or session credentials.

There is **no code for defeating a login, paywall, CAPTCHA, geo/KYC barrier,
rate limit or anti-bot challenge**, and adding any would be out of scope for
this desk. Proxy and stealth settings exist for reliability on sources the
operator is authorized to read.

### Going live

Live mode is opt-in twice — `--live` *and* a separate confirmation flag:

```bash
export BROWSERBASE_API_KEY=...      # names documented in .env.example
export BROWSERBASE_PROJECT_ID=...

npx tsx src/cli/pm-desk.ts source collect \
  --spec specs/sources/<your-spec>.yaml \
  --live --i-understand-this-uses-a-live-browserbase-session
```

A live run opens a billed remote browser session. Without both flags the
collector uses `--fixture`, and a missing fixture is an error rather than an
invented page.

---

## Live checks (opt-in)

Live suites are excluded from `npm test` by config *and* skip themselves unless
their environment variable is set — an accidental run is a no-op.

```bash
# Polymarket public endpoints. No credentials needed. Tiny limits.
PM_DESK_LIVE=1 npm run test:live

# Browserbase. Requires BOTH flags plus credentials. Bills a real session.
PM_DESK_LIVE=1 PM_DESK_LIVE_BROWSERBASE=1 npm run test:live
```

---

## Hermes integration

Everything in this section is **opt-in and off by default**. Nothing here reads
or writes Hermes' `config.yaml`, enables a webhook, or creates a cron job. The
desk talks to Hermes the way any other program would: by running the `hermes`
CLI.

Three distinct paths, deliberately kept separate:

| | What it is | Default |
|---|---|---|
| **(a) PM Desk local ingress** | The desk's own loopback HMAC server. Validates the envelope schema, records **before** dispatching, enforces idempotency. | on when you run `ingress serve`; dispatcher = `outbox` |
| **(b) Hermes native webhook** | A generic Hermes delivery route. Does none of the above validation. | off; global webhook config untouched, and the provisioner will not enable one |
| **(c) Hermes cron** | A schedule for the deterministic monitor sweep. | off, until `pm-desk provision apply` creates jobs for a plan Joe approved |

There is no agent polling loop anywhere. Detection is code.

### The three shipped workflows

```bash
npx tsx src/cli/pm-desk.ts hermes workflows
```

| Workflow | Role |
|---|---|
| **`pm-morning-generator-v0.yaml`** | **The product loop.** Morning idea → ExecutionPlan → Telegram gate. Run this one. |
| `pm-signal-adjudication-v0.yaml` | Optional subroutine: does one *already-fired* signal matter? One agent node, `tools: []`. |
| `pm-desk-paper-v0.yaml` | Superseded by the generator, kept for reference. Ends at a bare gate that nothing can act on, and its `condition:` edges cannot fire on this host (see the file's header). |

### Install the prompt libraries (required for the generator)

`pm_morning_generator_v0` uses Hermes' `spec.prompt: {library: <name>}` form.
Hermes resolves those names against `$HERMES_HOME/workflows/prompts/` and its own
builtin tree, and **hard-rejects an unknown library** — there is no
not-found-means-empty-prompt fall-through. Until the libraries this package ships
are installed, the generator does not validate:

```console
$ hermes workflow validate workflows/pm-morning-generator-v0.yaml
REJECTED
  [ERROR] PROMPT_LIBRARY prepare: unknown prompt library 'pm-prepare-context-v1' ...
```

The installer is **dry-run by default** and never touches a real `$HERMES_HOME`
on its own:

```bash
# 1. See exactly what would be written. Writes nothing.
npx tsx src/cli/pm-desk.ts hermes install-prompts

# 2. Do it.
npx tsx src/cli/pm-desk.ts hermes install-prompts --apply

# Target a specific profile home instead of $HERMES_HOME / ~/.hermes:
npx tsx src/cli/pm-desk.ts hermes install-prompts --hermes-home /path/to/home --apply
```

It refuses to overwrite a library you have edited locally unless you pass
`--force`. Afterwards (real output from this host):

```console
$ hermes workflow validate workflows/pm-morning-generator-v0.yaml
OK  workflow=pm_morning_generator_v0 hash=sha256:9efb9e6136c35b2b...
```

`pm_signal_adjudication_v0` needs none of this — its prompt is an inline string
that the launcher fills in.

---

## First live morning run

The one sequence to follow after merging. Steps 1–4 are Hermes; steps 5–8 are
the deterministic half.

```bash
export PATH="$HOME/.local/node-v24.18.1-linux-x64/bin:$PATH"   # Node >= 24
cd optional-projects/pm-desk && npm ci

# Pick your homes explicitly. Use throwaways the first time through.
export HERMES_HOME="$HOME/.hermes"          # or: $(mktemp -d)
export PM_DESK_HOME="$PWD/data"

# 1. Install the prompt libraries into that home (dry-run first if you like).
npx tsx src/cli/pm-desk.ts hermes install-prompts --hermes-home "$HERMES_HOME" --apply
hermes workflow validate workflows/pm-morning-generator-v0.yaml

# 2. Compile the morning's directive seed. Deterministic: same date + desk
#    state + nonce gives the same cards.
npx tsx src/cli/pm-desk.ts taxonomy compile --max-cards 3 --json > /tmp/seed.json

# 3. Wrap it into the workflow's input shape.
node -e '
  const s = JSON.parse(require("fs").readFileSync("/tmp/seed.json","utf8"));
  process.stdout.write(JSON.stringify({
    directive_cards: s.cards,
    seed: { seed_id: s.seed, run_date: s.run_date,
            taxonomy_version: s.taxonomy_version, desk_state_hash: s.desk_state_hash },
  }));
' > /tmp/seed-wrapped.json

# 4. Prove the wiring at zero cost — compiles and plans the ready set, spawns
#    no agent and calls no model.
hermes workflow run workflows/pm-morning-generator-v0.yaml \
  --input "$(cat /tmp/seed-wrapped.json)" --dry-run

#    Then the real run. This one costs money (max_budget_usd: 12.00) and ends
#    awaiting_gate, with the brief delivered to Telegram.
hermes workflow run workflows/pm-morning-generator-v0.yaml \
  --input "$(cat /tmp/seed-wrapped.json)"
```

Joe answers the gate in Telegram. If you are deciding from a terminal instead:

```bash
hermes workflow list
hermes workflow gate <run_id> paper_gate --decide approve   # or shelve / modify
```

Then the deterministic half:

```bash
# 5. Recover the plan from the run's node outputs.
npx tsx src/cli/pm-desk.ts plan from-run \
  --run-id <run_id> --hermes-home "$HERMES_HOME" --out /tmp/plan.json

# 6. Check it, and read exactly what was approved.
npx tsx src/cli/pm-desk.ts plan validate --file /tmp/plan.json
npx tsx src/cli/pm-desk.ts plan render-telegram --file /tmp/plan.json

# 7. Stamp the approval FROM HERMES' OWN RECORD. This reads
#    $HERMES_HOME/workflows/runs/<run_id>/gate_signals/paper_gate.json and
#    never writes it. Exits 1 if Joe did not approve.
npx tsx src/cli/pm-desk.ts plan approve \
  --file /tmp/plan.json --run-id <run_id> --hermes-home "$HERMES_HOME"

# 8. Preview every file and command. Writes nothing, spawns nothing.
npx tsx src/cli/pm-desk.ts provision dry-run \
  --plan /tmp/plan.json --hermes-home "$HERMES_HOME" --desk-home "$PM_DESK_HOME"

#    Install for real.
npx tsx src/cli/pm-desk.ts provision apply \
  --plan /tmp/plan.json --hermes-home "$HERMES_HOME" --desk-home "$PM_DESK_HOME" \
  --i-approved-this-plan
```

Afterwards:

```bash
npx tsx src/cli/pm-desk.ts provision status --plan-id <plan_id> --desk-home "$PM_DESK_HOME"
hermes cron list
npx tsx src/cli/pm-desk.ts provision revoke --plan-id <plan_id> --desk-home "$PM_DESK_HOME"
```

### Why steps 5–8 are a CLI bridge and not a workflow node

Hermes resolves a `run:` script node against a registry that is **frozen at
import** (`workflow/runtime/scripts.py`; mutation after bootstrap raises). On
this host it holds exactly: `best`, `concat`, `first_k`, `judge_converge`,
`majority`, `top_k`, `workflow.examples.echo`,
`workflow.examples.notify_telegram`. There is no `pm_desk.*` callable and this
package does not add one, because doing so would mean editing Hermes core.

So a YAML node reading `run: pm_desk.provision` would simply fail to compile.
The bridge is the CLI, exactly as it is for the paper ledger. That is a real
seam, not a hidden one: the workflow's job ends when it has produced a
schema-valid artifact, and a separate program acts on it.

The upside is that the gate means something. The provisioner is a different
process, run deliberately, that re-validates the plan and refuses to apply
anything Hermes did not record an approval for.

### What the provisioner will not do

- **Enable a Hermes webhook.** A `webhook_subscribe_recipe` item is printed for
  you to run or ignore. Enabling one edits your Hermes config; this package
  does not.
- **Run a catalog workflow.** A `catalog_run` item is printed for the same
  reason — it spends model budget, which is your call.
- **Execute a command a model wrote.** Every argv is derived from the plan's
  validated monitors. Disagreement with the plan's display text is reported as
  drift, not obeyed.
- **Touch a home you did not name.** `--hermes-home` falls back to `$HERMES_HOME`
  then `~/.hermes`, exactly as Hermes resolves it, and every write goes there.
- **Delete a cron job it did not create.** Revoke uses the job ids recorded at
  creation; it never lists your jobs and prefix-matches.

### (a) Local ingress → adjudication

By default the ingress uses the **outbox dispatcher**: it writes the envelope to
the content-addressed store, queues a row, and invokes nothing. Handing a signal
to an adjudicator is a separate, explicit step.

#### Manual

```bash
npx tsx src/cli/pm-desk.ts ingress outbox --status queued --json

hermes workflow validate workflows/pm-signal-adjudication-v0.yaml
hermes workflow run workflows/pm-signal-adjudication-v0.yaml \
  --input "$(cat /path/to/rendered-input.json)" --dry-run

npx tsx src/cli/pm-desk.ts workflow adjudicate --result result.json
```

#### Automatic launcher (still local, still opt-in)

```bash
export PM_DESK_DISPATCH_HERMES=1
npx tsx src/cli/pm-desk.ts ingress serve --dispatch hermes

# Prove the wiring first, at zero cost — compiles and plans, spawns no agent:
export PM_DESK_HERMES_DRY_RUN=1
```

The launcher runs `hermes workflow run <path> --input '<json>'` via `execFile`
(argv array, never a shell string), defaults to the packaged workflow by
absolute path, records failures as `failed` outbox rows so nothing is lost, and
**never reads or writes global Hermes configuration**. Override the target with
`PM_DESK_HERMES_WORKFLOW`.

#### Who writes the ledger

The workflow **cannot** write this desk's SQLite ledger, and nothing here
pretends otherwise. Hermes resolves workflow `run:` callables against a frozen
allowlist (`best`, `concat`, `first_k`, `judge_converge`, `majority`, `top_k`,
`workflow.examples.echo`, `workflow.examples.notify_telegram`), so a
`pm_desk.record` script node would not resolve. The boundary is:

```
pm-desk (deterministic code)  →  renders the prompt, hands it over as input
hermes workflow               →  produces a schema-validated adjudication artifact
pm-desk workflow adjudicate   →  validates it again, and ONLY a `paper_alert`
                                 becomes a paper ledger row
```

`pm-desk workflow adjudicate --result <file>` is that bridge. It re-parses the
result against the Adjudication schema, refuses a signal it has never seen, and
still enforces every ledger invariant — the offline demo shows it declining to
invent an entry price when no market observation backs the signal.

### Workspaces (what the primitive actually is)

A Hermes workspace is a **named persistent directory at
`$HERMES_HOME/workflows/workspaces/<name>/`** that a workflow's agent nodes read
and write with their ordinary file tools. Two things it is not, both of which
would be easy to assume:

- **It is not a bind of this package's directory.** There is no supported
  primitive for pointing a workflow at an arbitrary external path. If a node
  needs a file from here, it must be handed the content, not a mount.
- **It is not a per-node isolation boundary.** `spec.workspace` and
  `spec.profile` are override-only fields; `hermes workflow validate` rejects
  them outright precisely because no execution path enforces them.

So `pm_desk_paper_v0` declares `workspace: pm-desk` (its nodes hold `read_file`
and can use it), and `pm_signal_adjudication_v0` deliberately does **not** — a
`tools: []` node has no file tools, and pinning a workspace it cannot open would
create an empty directory in your Hermes home and imply a binding that does not
exist.

Hermes creates the directory on first real run; there is nothing to set up.
Inspect it with:

```bash
ls "${HERMES_HOME:-$HOME/.hermes}/workflows/workspaces/pm-desk/"
```

### (b) Hermes webhooks — documented, not enabled

**Hermes webhooks are NOT enabled on this host, and this desk does not enable
them.** The commands below are documented for review; run them yourself if and
when you want that route.

```bash
# 1. Inspect current subscriptions.
hermes webhook list

# 2. Create a route that only delivers, with no agent and no LLM cost.
hermes webhook subscribe pm-desk-signal \
  --description "PM Desk signal envelopes (paper-only)" \
  --deliver telegram \
  --deliver-only \
  --secret "$PM_DESK_INGRESS_SECRET" \
  --prompt "PAPER ONLY signal {signal_id}: {kind} / {severity} — rule {rule_id}"

# 3. Verify the route before pointing anything at it.
hermes webhook test pm-desk-signal

# 4. Remove it again.
hermes webhook remove pm-desk-signal
```

The desk's own ingress remains the recommended path: it validates the envelope
schema, records before dispatching, and enforces idempotency — none of which a
generic webhook route does.

### (c) Scheduling — opt-in, script-first, no agent

Detection needs no model, so the scheduled unit is a **script**, not an agent
turn. `scripts/monitor-sweep.sh` evaluates every monitor spec and prints
**nothing** when nothing fired; `hermes cron create --no-agent` treats empty
stdout as silence, so a quiet desk costs zero tokens and sends zero
notifications.

**No cron job is created by this repository.** Run this yourself if you want it:

```bash
# Hermes runs scripts from ~/.hermes/scripts/.
mkdir -p ~/.hermes/scripts
cp scripts/monitor-sweep.sh ~/.hermes/scripts/pm-desk-monitor-sweep.sh

# Dry it once by hand first.
PM_DESK_DIR="$PWD" ~/.hermes/scripts/pm-desk-monitor-sweep.sh

hermes cron create '30m' \
  --name pm-desk-monitor-sweep \
  --script pm-desk-monitor-sweep.sh \
  --no-agent \
  --deliver local

hermes cron list
hermes cron remove pm-desk-monitor-sweep
```

`--no-agent` is load-bearing: without it the script's stdout would be injected
into an agent prompt every 30 minutes. With it, the script *is* the job. The
sweep observes and reports; it never starts a workflow and never trades.

### Register the workflow in the catalog (optional)

```bash
hermes workflow register workflows/pm-signal-adjudication-v0.yaml
hermes workflow list-catalog

export PM_DESK_HERMES_MODE=catalog
export PM_DESK_HERMES_WORKFLOW=pm-signal-adjudication-v0
```

---

## Paper-only boundary

This is enforced in four independent places, so removing any one of them still
leaves the desk unable to act:

1. **Dependency and import guard** — `tests/guard.test.ts` scans every file in
   `src/` (comments stripped) for trading, signing and wallet symbols and
   packages, and asserts that exactly one module imports `@polymarket/client`,
   importing exactly one symbol: `createPublicClient`. No `ethers`, `viem` or
   signer package is even installed.
2. **Schema** — `paper_only` is a literal `true` in the SignalEnvelope and
   Adjudication schemas. A payload asserting anything else is rejected at every
   boundary. The adjudication decision set is closed at three research outcomes;
   there is no field anywhere that could express an order, a venue action or a
   wallet.
3. **Database** — `paper_ledger` has `CHECK (paper_only = 1)` and
   `CHECK (fill_type = 'SIMULATED_NO_FILL')`. A test asserts SQLite itself
   rejects `UPDATE ... SET fill_type='REAL_FILL'`.
4. **Capability** — the adjudication workflow's agent node has `tools: []`. It
   has no terminal, no browser and no network, so it cannot reach a venue even
   if something else failed.

The collector additionally refuses Polymarket hosts and every transactional
route pattern, and never persists cookies.

### What live execution would require (not built, not started)

Turning this into something that could trade is a separate system, not a flag:

- wallet and key custody with a real threat model, and signing isolated from any
  LLM-reachable process;
- eligibility: jurisdiction, geo and KYC verification for the operating entity;
- an execution adapter with idempotent order submission, reconciliation, and
  handling for partial fills and rejects;
- position, sizing and loss limits enforced **outside** prompts, in code that an
  agent cannot edit or talk its way around;
- dual control for anything that moves funds, and a tamper-evident audit trail;
- a kill switch that survives the agent layer being wedged;
- paper-vs-live divergence measurement before believing any of it.

None of that is seeded here, and none of the current code is a step toward it.
The ledger is deliberately structured so a paper row can never be mistaken for a
fill.

---

## Known limitations

- **The live Browserbase path has not been exercised against a real session.**
  It is fully unit-tested behind a fake browser, and the live check is written
  and double-gated, but running it bills a session and was not requested. The
  first live run may surface selector-timing issues no fixture can predict.
- **Realtime is polling.** The official client exposes public subscriptions
  (`market capability` reports this), but the desk polls: it is deterministic
  and cannot silently stall the way a dropped socket can. `SubscriptionSeam` in
  `src/polymarket/realtime.ts` documents the contract a subscription feed must
  satisfy to replace it.
- **`example_market_move.yaml` ships disabled** with a placeholder token. No
  live market token is bound by default; binding one is a deliberate operator
  action.
- **No workflow here has been run against a live model.** Both the generator and
  the adjudicator validate (`hermes workflow validate` → OK) and compile and
  plan under `hermes workflow run --dry-run`, but no run has ever reached an
  LLM. Their output contracts are exercised offline against the fixtures in
  `fixtures/plans/` and `fixtures/adjudication/`. Until a live morning happens,
  treat decision quality as unmeasured — and expect the first real run's plan
  JSON to need a hand-fix or two before `plan validate` accepts it.
- **A node's declared `spec.output` schema is not enforced by Hermes.** It is
  stored in the IR and no execution path validates against it (checked against
  `workflow/verify.py` and `runtime/live.py` here). The plan node declares one
  as documentation; the real enforcement is `pm-desk plan validate`, after the
  fact. A plan that fails it cannot be repaired by the workflow — the morning
  is simply wasted.
- **DD is one agent, not a fanout.** An agent node's output is the envelope
  `{text, node, result, ...}` with the model's JSON as a string inside `text`,
  never parsed. So `over: "{{ dq.output.survivors }}"` raises TemplateError and
  fails the node, and `condition: "$.decision == 'advance'"` on an edge out of
  an agent fails closed to "blocked", silently skipping everything downstream.
  Verified against `workflow/expr.py` on this host. Parallel DD becomes possible
  if Hermes ever surfaces parsed agent JSON into node output; until then a
  fanout there would be a workflow that crashes on its first real run.
- **The provisioner cannot be a workflow node.** Hermes' script registry is
  frozen at import and holds no `pm_desk.*` callable, so the bridge is the CLI.
  See [Why steps 5–8 are a CLI bridge](#why-steps-58-are-a-cli-bridge-and-not-a-workflow-node).
- **Not ready for deployment.** Beyond the unexercised live paths above, there
  is no operator alerting on desk failure, no retention policy for the artifact
  store, and no measurement of paper-vs-live divergence. Run it as a research
  instrument, not as infrastructure.
- **Deterministic diffing is textual.** A source that reorders content without
  changing meaning will fingerprint as changed; the adjudicator is the layer
  that catches that, at the cost of one LLM call.
