# PM Desk MVP — build summary

Branch `feat/pm-desk-mvp`, 11 commits on top of `8280ee7`. Nothing pushed, no
PR opened, no remote created.

---

## Architecture in one paragraph

Two bands, separated on purpose. The **deterministic band** — official
Polymarket public SDK adapter, Browserbase primary-source collector, SQLite
evidence store with content-addressed artifacts, and a declarative monitor
engine — contains no LLM anywhere. It detects change, hashes the evidence, and
emits at most one schema-valid `SignalEnvelope`. That envelope crosses into the
**judgement band** through a loopback HMAC ingress that records before it
dispatches. The judgement band is a single agent node with `tools: []` that
chooses `ignore` / `watch` / `paper_alert`. A `paper_alert` writes a paper ledger
row whose `paper_only` and `fill_type` columns are CHECK-constrained, and renders
a Telegram-ready message with `PAPER ONLY` on the first line. Agents never own a
polling loop and never get tools to discover whether something changed.

---

## What was built (six deliverables)

| # | Deliverable | Where | State |
|---|---|---|---|
| A | Official Polymarket public SDK adapter | `src/polymarket` | Done; live smoke passed |
| B | Normalized store + evidence artifacts | `src/store` | Done |
| C | Deterministic monitor engine | `src/monitor` | Done; 4 rule kinds |
| D | Browserbase primary-source adapter | `src/browserbase` | Done offline; live path **not exercised** |
| E | Local ingress + Hermes wake-up | `src/ingress` | Done; launcher opt-in, off by default |
| F | Paper ledger + alert presentation | `src/ledger` | Done |

Plus: versioned directive taxonomy and deterministic seed compiler
(`taxonomy/cards.yaml`, `src/taxonomy`), the `pm_signal_adjudication_v0`
workflow, and a refactor of the broader research spine.

---

## Actual verification output

All commands run from `/home/hermes/pm-desk` on this host.

### `npm test` — full offline suite

```text
 ✓ tests/browserbase.test.ts (33 tests) 1184ms
 ✓ tests/taxonomy.test.ts   (21 tests)  617ms
 ✓ tests/ingress.test.ts    (24 tests)  419ms
 ✓ tests/monitor.test.ts    (30 tests)  285ms
 ✓ tests/e2e.test.ts         (2 tests)  202ms
 ✓ tests/ledger.test.ts     (17 tests)  146ms
 ✓ tests/polymarket.test.ts (16 tests)   93ms
 ✓ tests/store.test.ts      (17 tests)  129ms
 ✓ tests/schema.test.ts     (20 tests)   46ms
 ✓ tests/guard.test.ts       (5 tests)   35ms
 ✓ tests/core.test.ts       (13 tests)   17ms

 Test Files  11 passed (11)
      Tests  198 passed (198)
```

No test touches the network. Live suites are excluded by config *and* self-skip.

### `npm run lint` / `npm run typecheck` / `npm run build`

```text
===== LINT =====      (clean, no output)
===== TYPECHECK =====  (clean, no output)
===== BUILD =====
> tsc -p tsconfig.build.json
dist/{browserbase,cli,core,ingress,ledger,monitor,polymarket,schema,store,taxonomy,workflow}
```

### Live public-SDK smoke — **opt-in, no credentials**

```text
$ PM_DESK_LIVE=1 npm run test:live

 ✓ tests/live/polymarket-smoke.test.ts (3 tests) 354ms
 ↓ tests/live/browserbase-smoke.test.ts (1 test | 1 skipped)

 Test Files  1 passed | 1 skipped (2)
      Tests  3 passed | 1 skipped (4)
```

And through the CLI against the real public endpoints:

```text
$ npx tsx src/cli/pm-desk.ts market discover --limit 3
stored 3 markets / 6 outcome tokens (1 page(s))
market_id  question                                 tokens
---------  ---------------------------------------  ------
540817     New Rihanna Album before GTA VI?         2
540818     New Playboi Carti Album before GTA VI?   2
540819     Will Jesus Christ return before GTA VI?  2

$ npx tsx src/cli/pm-desk.ts market snapshot --token 98022490269692409998...
appended 1 observation(s)
token_id              observed_at               mid    spread
98022490269692409...  2026-07-31T00:20:57.515Z  0.505  0.01

$ npx tsx src/cli/pm-desk.ts market capability
client            sdk (public, read-only)
subscriptions     available
active data path  polling
```

Real market ids, questions, `conditionId`s, midpoint and spread — no credentials
involved.

### Workflow validation against the installed Hermes

```text
$ hermes workflow validate workflows/pm-signal-adjudication-v0.yaml
OK  workflow=pm_signal_adjudication_v0 hash=sha256:ac6d4d1d5119a80b...

$ hermes workflow validate workflows/pm-desk-paper-v0.yaml
OK  workflow=pm_desk_paper_v0 hash=sha256:1b620a76f7cf39e92fd2d6f...
```

### Offline end-to-end demo

`./scripts/demo-offline.sh` runs the full loop through the CLI (exit 0). It
includes the ingress over real loopback HTTP:

```text
=== 7. Start the local ingress (127.0.0.1 only) and submit the signal ===
HTTP 202
{ "status": "accepted", "signal_id": "sig_0b1320e964c4d5c84507b6418de0479f",
  "dispatched": true, "dispatcher": "outbox", "paper_only": true }

=== 8. Inspect the durable outbox ===
id  signal_id                             status  dispatcher  queued_at
1   sig_0b1320e964c4d5c84507b6418de0479f  queued  outbox      2026-07-31T00:10:59.267Z

=== 10. Adjudicate — and watch the ledger REFUSE to invent an entry price ===
LEDGER_INVARIANT_ERROR: slippage_rule cross_spread_full needs a market snapshot,
which this entry observation does not have
```

That refusal is the invariant working: the sample monitor ships bound to no
market, so the signal carries no snapshot, and the ledger will not guess an entry
price. The full adjudication → ledger path *with* a snapshot is covered offline
by `tests/e2e.test.ts`.

---

## Three things that were wrong until checked against the real host

Worth calling out, because each would have failed on first real use:

1. **`hermes workflow run` has no `--input-file` flag.** The real shape is
   `run <path.yaml> --input '<json>'`. The launcher was rewritten to emit that,
   and to support `run-catalog` as well.
2. **Workflow `run:` callables resolve against a frozen allowlist** holding only
   `['best','concat','first_k','judge_converge','majority','top_k',
   'workflow.examples.echo','workflow.examples.notify_telegram']`. The first
   four-node adjudication workflow referenced `pm_desk.*` callables and was
   **REJECTED**. It is now a single agent node; validate/render/record stay in
   deterministic pm-desk code, which is where they belonged anyway.
3. **`spec.deny_tools` is enforced by no execution path** (Hermes warns about
   this). A decorative deny-list next to a real allowlist is worse than none, so
   the restriction is expressed positively as `tools: []`.

---

## Paper-only enforcement

Four independent mechanisms; removing any one still leaves the desk unable to
act:

1. **Guard test** (`tests/guard.test.ts`) scans every file in `src/` with
   comments stripped for trading/signing/wallet symbols and packages, and
   asserts exactly one module imports `@polymarket/client`, importing exactly
   one symbol: `createPublicClient`. No `ethers`/`viem`/signer package is
   installed at all.
2. **Schema** — `paper_only` is a literal `true`; the adjudication decision set
   is closed at three research outcomes, and no field anywhere can express an
   order, venue action or wallet.
3. **Database** — `CHECK (paper_only = 1)` and
   `CHECK (fill_type = 'SIMULATED_NO_FILL')` on `paper_ledger`. A test asserts
   SQLite itself rejects `UPDATE ... SET fill_type='REAL_FILL'`.
4. **Capability** — the adjudicator's agent node has `tools: []`: no terminal,
   no browser, no network.

The Browserbase collector additionally refuses Polymarket hosts outright, plus
wallet/auth/account/checkout/payment/trading/order/deposit/KYC route patterns,
and never persists cookies. There is no code for defeating a login, paywall,
CAPTCHA, geo/KYC barrier, rate limit or anti-bot challenge.

---

## Limitations (things a reviewer should not assume work)

- **The live Browserbase path has never run against a real session.** It is
  fully unit-tested behind a fake browser and the live check is written and
  double-gated (`PM_DESK_LIVE=1 PM_DESK_LIVE_BROWSERBASE=1` plus credentials),
  but running it bills a session and was not requested. The first live run may
  surface selector-timing behaviour no fixture can predict.
- **The adjudication workflow has never run against a live model.** It validates
  clean; its output contract is exercised offline through
  `fixtures/adjudication/paper_alert.json`.
- **Realtime is polling.** The official client does expose public subscriptions
  (`market capability` reports `subscriptions: available`), but the desk polls
  deliberately — polling is deterministic and cannot silently stall.
  `SubscriptionSeam` in `src/polymarket/realtime.ts` is the documented contract
  for replacing it; it is a type, not a stub, so nothing pretends to be
  implemented.
- **No live market token is bound.** `specs/monitors/example_market_move.yaml`
  ships `enabled: false` with a placeholder token id.
- **Hermes webhooks were not enabled and global Hermes config was not touched.**
  The exact opt-in commands are documented in the README for the user to run
  after review.
- **Diffing is textual.** A source that reorders content without changing meaning
  fingerprints as changed; the adjudicator is the layer that catches that, at the
  cost of one LLM call.
- **`npm audit` reports 5 high-severity advisories** in the transitive dependency
  tree (inherited via the SDKs). Not triaged; worth a look before any wider
  deployment.

---

## Commits

```text
2b54d6b docs(pm-desk): README and refactored research spine
8b25fe5 fix(pm-ingress): match the real Hermes CLI and allowlist constraints
beb739f test(pm-desk): offline end-to-end loop and CLI demo
95f43cd feat(pm-desk): directive taxonomy, seed compiler and adjudication workflow
67d3369 feat(pm-ledger): paper ledger with structural no-fill invariants and alert renderer
fde4a4e feat(pm-ingress): local-only HMAC signal ingress with opt-in Hermes launcher
c5c1c02 feat(pm-monitor): deterministic rule engine with dedupe and cooldown
0afa3b4 feat(pm-source): deterministic Browserbase primary-source collector
fec3dd3 feat(pm-data): official Polymarket public SDK adapter and bounded collector
e80f0c5 feat(pm-store): sqlite evidence store with content-addressed artifacts
3e5a927 feat(pm-desk): typed core primitives and boundary schemas
```

`git diff main...HEAD --stat`: 93 files changed, ~16.5k insertions, 149
deletions. The deletions are the replaced Python `urllib` watcher scaffold,
which the official-SDK data plane supersedes.

Nothing committed contains a secret, a database, an artifact directory or a
Browserbase recording; `.env.example` documents variable **names** only.

---

## Method note

Built test-first: each vertical behaviour got a focused failing test, was run and
observed failing, then implemented and re-run green. Several failures were real
design findings rather than typos, and were fixed as such:

- markup reflow changed a source fingerprint — HTML has no reliable line
  semantics, so extraction now canonicalizes all whitespace, and a cosmetic edit
  no longer pages an operator;
- ingress idempotency keyed on *record* meant every monitor-emitted signal
  arrived as a duplicate and never reached an adjudicator; it now keys on
  *dispatch*;
- an oversized body reset the connection instead of answering `413`;
- the ledger's "missing price input" hint recommended the slippage rule the
  caller had already chosen.
