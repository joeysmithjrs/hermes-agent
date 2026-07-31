# PM Desk

A **local-first, paper-only, artifact-driven** prediction-market research and
alerting desk.

It watches named primary sources and official public market data, detects
material changes with deterministic code, and hands a single evidence-backed
signal to a narrow LLM adjudicator that decides `ignore` / `watch` /
`paper_alert`. A `paper_alert` writes a row to a local paper ledger and renders
a Telegram-ready message.

> **PAPER ONLY.** This desk cannot trade, sign, connect a wallet, place an order,
> or participate in UMA. That is enforced structurally — by an empty tool
> allowlist, by database CHECK constraints, and by a guard test that scans all of
> `src/` — not by convention. See [Paper-only boundary](#paper-only-boundary).

---

## Architecture

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

Two properties hold this together:

1. **Agents never own a polling loop and never get tools to discover change.**
   Collectors and monitors are deterministic code. By the time a model is
   invoked, the change is already detected and its evidence already hashed and
   stored.
2. **Every LLM claim is traceable.** The adjudicator has `tools: []`, so it
   cannot look anything up. Anything it asserts must come from the rendered
   evidence, which is content-addressed and re-verifiable.

---

## Layout

```text
src/core         typed errors, UTC time, content hashing, deterministic ids
src/schema       zod schemas: SignalEnvelope, SourceSpec, MonitorSpec, Adjudication
src/store        SQLite migrations, repositories, content-addressed artifacts
src/polymarket   official public SDK adapter, normalizer, snapshots, polling feed
src/browserbase  navigation policy, deterministic extraction, live/fixture browsers
src/monitor      declarative rules, diffing, dedupe/cooldown engine
src/ingress      HMAC auth, local HTTP server, dispatchers
src/ledger       paper ledger, invariants, Telegram renderer
src/taxonomy     versioned directive cards + deterministic seed compiler
src/workflow     adjudication prompt rendering
src/cli          pm-desk command-line entry point
specs/           sample SourceSpecs and MonitorSpecs
fixtures/        offline HTML + adjudication fixtures (make the E2E runnable)
taxonomy/        cards.yaml — the versioned directive taxonomy
workflows/       Hermes workflow definitions
tests/           unit + guard + end-to-end; tests/live/ is opt-in only
```

---

## Setup

```bash
npm install
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
npm test           # full offline suite; never touches the network
npm run lint       # eslint (typed rules)
npm run typecheck  # tsc --noEmit, strict
npm run build      # tsc -> dist/
npm run verify     # lint + typecheck + test

npm run test:live  # OPT-IN only; see "Live checks" below
```

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

## Hermes handoff (opt-in, disabled by default)

By default the ingress uses the **outbox dispatcher**: it writes the envelope to
the content-addressed store, queues a row, and invokes nothing. Handing a signal
to an adjudicator is a separate, explicit step.

### Option A — manual launcher

```bash
npx tsx src/cli/pm-desk.ts ingress outbox --status queued --json

hermes workflow validate workflows/pm-signal-adjudication-v0.yaml
hermes workflow run workflows/pm-signal-adjudication-v0.yaml \
  --input "$(cat /path/to/rendered-input.json)" --dry-run

npx tsx src/cli/pm-desk.ts workflow adjudicate --result result.json
```

### Option B — automatic launcher (still local, still opt-in)

```bash
export PM_DESK_DISPATCH_HERMES=1
export PM_DESK_HERMES_WORKFLOW=workflows/pm-signal-adjudication-v0.yaml
npx tsx src/cli/pm-desk.ts ingress serve --dispatch hermes
```

The launcher runs `hermes workflow run <path> --input '<json>'` via `execFile`
(argv array, never a shell string), records failures as `failed` outbox rows so
nothing is lost, and **never reads or writes global Hermes configuration**.

### Option C — Hermes webhooks

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
- **The adjudication workflow has not been run against a live model.** It
  validates (`hermes workflow validate` → OK), and its output contract is
  exercised offline via the fixture in `fixtures/adjudication/`.
- **Deterministic diffing is textual.** A source that reorders content without
  changing meaning will fingerprint as changed; the adjudicator is the layer
  that catches that, at the cost of one LLM call.
