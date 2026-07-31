# PM Desk MVP — Full Build Directive

## Mission

Build a **local-first, paper-only, artifact-driven prediction-market research and alerting desk** in this repository. Implement the six components below as a cohesive, runnable system—not a mockup, design document, placeholder, or a collection of unconnected scripts.

The target loop is:

```text
seeded directive taxonomy
→ official Polymarket public data collector
→ deterministic Browserbase primary-source collector
→ normalized append-only evidence / market store
→ script-first monitor evaluates predicates + dedupe
→ material SignalEnvelope only
→ local webhook ingress
→ Hermes signal-adjudication workflow
→ concise Telegram-deliverable evidence-backed decision
→ paper ledger entry
```

The desk is research/paper-only. **Do not build live execution, wallet connection, signing, secure Polymarket client authentication, order placement, browser login, UMA propose/dispute, or any system that may trade.**

## Repository and execution context

- Working repo: `/home/hermes/pm-desk`
- Branch: create and work on `feat/pm-desk-mvp` from current `main`.
- Current baseline commit: `9ee8053` (`chore(pm-desk): seed paper-only desk scaffold`). Preserve useful scaffold ideas but refactor/replace as needed.
- Hermes installation: `/opt/hermes-agent`, workflow runtime is enabled and has `workflow` CLI.
- Hermes home: `/home/hermes/.hermes`; reusable prompt libraries live in `/home/hermes/.hermes/workflows/prompts/`.
- Existing workflow catalog entry `pm-desk-paper-v0` is just a preliminary scaffold; evolve it deliberately or replace it with versioned workflow files in this repo. Do not rely on undocumented artifacts.
- Node 22 and npm 10 are installed. Python 3.11 is available. Prefer TypeScript/Node for official Polymarket + Browserbase integration; Python is fine for local helper/CLI if it is the cleanest choice.
- A `BROWSERBASE_API_KEY`, `BROWSERBASE_PROJECT_ID`, `BROWSERBASE_PROXIES`, and `BROWSERBASE_ADVANCED_STEALTH` exist in `/home/hermes/.hermes/.env`. Never print, copy, commit, or expose their values. Provide `.env.example` containing names only.
- `claude-pro` route is Claude Pro OAuth; do not change Hermes or Claude global config.

## Authoritative integration facts verified in a real host probe

### Polymarket

- Use the current official package: `@polymarket/client` (public client only).
- A live host smoke succeeded with:

```ts
import { createPublicClient } from '@polymarket/client';
const client = createPublicClient();
const pages = client.listMarkets({ closed: false, pageSize: 3 });
const page = await pages.firstPage();
```

and returned real market records including IDs, questions, slugs, and `conditionId`.
- The public client exposes discovery, market detail, midpoint, price, spread, books, history, trades/analytics, and public subscription capabilities. Use official package APIs/types—not a reverse-engineered raw REST wrapper as the primary production path.
- The old direct Python `urllib` access to Gamma/CLOB returned 403 from this host. This is why the official SDK is the primary data plane. If an SDK method cannot access something, surface a typed capability/availability result; do not bolt on restriction-evasion behavior.
- Do not import/use `createSecureClient`, signer packages, trading actions, credentials, secure subscriptions, wallet actions, or any order function in production source. A guard test should enforce this.

### Browserbase

- Browserbase is for deterministic, code-owned collection of **named primary sources** with declared URLs/selectors/extraction rules, not free-form agent browsing and not Polymarket trading.
- Use official `@browserbasehq/sdk` and a supported deterministic browser automation approach (e.g. Playwright connected to a Browserbase session) where appropriate.
- Use configured proxy/advanced-stealth settings only for reliability on sources the user is authorized to access. Never implement bypass of logins, paywalls, KYC/geo barriers, rate limits, CAPTCHA, terms restrictions, or anti-bot challenges.
- Browserbase collector must not visit wallet/signing/trade routes and must not persist cookies/session credentials.
- Make the Browserbase adapter dependency-injected and fully testable with a fake browser/session client; live Browserbase calls must be opt-in via explicit CLI flag/environment guard.

### Hermes

- Workflow Dispatch supports YAML DAGs, fanout/map, join/reducers, conditions, catalog, checkpoints, toolset narrowing, and gates.
- Native workflow scripts `run:` resolve against a frozen allowlist. Do not assume arbitrary local scripts can be invoked as workflow `run:` nodes without explicitly integrating/allowlisting them. For MVP use a shell/Node/Python launcher plus Hermes webhook/cron mechanics where appropriate, or document a minimal future integration seam.
- Hermes webhooks are currently NOT enabled. Do not modify global Hermes config or enable a public listener. Implement a local-only ingress/handler and provide exact opt-in setup commands plus tests. The user will enable it separately after review.

## Non-negotiable architecture

### 1. Data plane before agents

Collectors and monitors must be deterministic code. LLM/workflow nodes consume immutable evidence artifacts and structured signal envelopes. Agents never own an indefinite polling loop and never get broad terminal/browser access merely to discover whether something changed.

### 2. Local evidence store

Implement a local SQLite (preferred) or clear append-only JSONL store with migrations/init. It must persist:

- normalized Polymarket market metadata and outcome tokens;
- timestamped market observations / BBO or midpoint / orderbook summary when available;
- primary-source snapshots: URL, collected time, normalized extracted content, content hash, raw artifact path/hash, source spec version;
- monitor state/cooldown/dedupe decisions;
- `SignalEnvelope` records;
- paper-ledger entries and later annotations/markouts.

Design tables and public types deliberately. Use UTC ISO-8601/epoch consistently. Provide content-addressed raw artifact retention without storing secrets. Include a simple query/export CLI.

### 3. Strict SignalEnvelope

Define and validate a versioned schema/type, e.g.:

```json
{
  "version": 1,
  "signal_id": "sig_...",
  "kind": "primary_source_change|market_move|source_market_divergence",
  "severity": "info|warn|high|critical",
  "observed_at": "UTC timestamp",
  "rule_id": "...",
  "rule_version": "...",
  "market_refs": [{"market_id":"...","condition_id":"...","token_id":"...","outcome":"YES"}],
  "source_refs": [{"source_id":"...","url":"...","previous_hash":"...","current_hash":"...","artifact_ref":"..."}],
  "market_snapshot": {"observed_at":"...","mid":0.0,"best_bid":0.0,"best_ask":0.0,"spread":0.0},
  "evidence": {"diff_excerpt":"...","claims":["..."]},
  "paper_only": true,
  "dedupe_key": "..."
}
```

Adjust fields as necessary, but validate schemas at all boundaries. Include an explicit `paper_only: true` invariant. Signals must include provenance sufficient for an adjudicator to audit, not a vague LLM summary.

### 4. No LLM in detector path

Each monitor is a declarative rule spec. Engine accepts stored observations/snapshots, calculates predicates, dedupes, and either records no signal or emits a valid signal. Rules to include:

- primary source fingerprint/content change;
- absolute/relative midpoint movement;
- spread widening / missing book data;
- source changed while a linked market is active and within horizon;
- cooldown and idempotent dedupe.

No agent calls in this path. A dry run must be deterministic and repeatable.

### 5. Artifact-driven workflow

Create a separate `pm_signal_adjudication_v0` workflow that is triggered by a signal envelope, not a broad polling/research workflow. It should:

- validate input as signal schema;
- render a narrow prompt from the signal plus referenced evidence;
- decide only among `ignore`, `watch`, `paper_alert`;
- assess market/source semantic alignment, novelty, resolution/rules mapping, still-live state, and invalidation;
- produce strict structured output and a Telegram-ready concise message;
- write or request a paper ledger entry only for `paper_alert`;
- have no execution/trading capability and no terminal/browser access.

Keep any existing broader `prepare → seed → directive → DQ → DD → eval` research spine, but refactor it so it uses bounded directive cards and evidence artifacts. Do not need to run it during implementation.

## Build scope: the six deliverables

### A. Official Polymarket public SDK adapter

Implement a TypeScript module/package with a CLI that:

- creates an official public client only;
- discovers active markets with bounded paging/filtering;
- normalizes market, outcome token, and condition IDs;
- retrieves a snapshot (midpoint, price/spread/orderbook summary when supported); 
- retrieves history where supported;
- offers a reconnecting public realtime subscription abstraction if official SDK APIs permit it; otherwise cleanly ship a polling adapter with clear interface and a future subscription seam;
- upserts data into evidence store;
- supports a bounded `--limit`, `--dry-run`, and `--json` mode;
- has no side effects outside local store/artifact paths.

Do an **opt-in live SDK smoke** against a public endpoint, with a very small limit, and record actual command/output in final report. It must not need credentials.

### B. Normalized local store + evidence artifacts

Implement init/migrate, schema/types, repositories, hashes, snapshots, source artifacts, signal records, monitor state, paper ledger, and query/export CLI. Tests should use temporary DB/filesystem and prove persistence/retrieval/dedupe.

### C. Deterministic monitor engine

Implement declarative YAML/JSON monitor specs and engine. Include sample specs for primary-source change and market move but with no live token configured by default. Build a CLI that consumes a spec and stored fresh observations, prints `SILENT` on no event, and emits schema-valid JSON only on an event. Include tests for threshold, diff, cooldown, dedupe, stale/missing data, and repeatability.

### D. Browserbase primary-source adapter

Implement a deterministic `SourceSpec` format:

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

Requirements:
- validate HTTPS and allowlisted domain before starting a session;
- reject sensitive/disallowed route patterns (wallet, login, checkout, trade etc.); 
- start a configured Browserbase session only in explicit live mode;
- extract declared selectors/fields only;
- normalize content deterministically; hash, compare, persist artifact and provenance;
- never use agent-generated selectors on the fly;
- fake client tests cover all logic and a live check must be opt-in/skip safely without Browserbase credentials;
- provide a CLI `collect-source` with `--spec`, `--dry-run`, and `--live` requiring an explicit confirmation flag.

### E. Local ingress + Hermes wake-up integration

Build a local-only HTTP signal ingress (bind `127.0.0.1` by default) that:

- accepts only schema-valid envelopes;
- authenticates with a locally generated/configured HMAC secret passed via environment (do not log it); 
- has idempotency keyed by `signal_id`/`dedupe_key`;
- records first;
- invokes an injectable downstream dispatcher only for new valid signals;
- default dispatcher writes a durable queued event / outbox artifact; it must not silently invoke an LLM.

Provide an **opt-in** dispatcher/launcher to start `hermes workflow run <workflow>` or documented `hermes webhook` route with a strict JSON input file. It must be disabled by default, require explicit flag/env, be testable through a fake command runner, and never modify global Hermes config. Document exact commands to enable the official Hermes webhook layer after user approval.

### F. Paper ledger + alert presentation

Implement a paper ledger that can be created only from a validated `paper_alert` adjudication or explicit manual operator command. Capture: thesis hash, linked signal/evidence, decision timestamp, candidate market/tokens, entry observation, assumed size, specified slippage rule, expiry/markout horizons, invalidations, later outcomes/notes. It must be impossible to label this a real fill.

Create Telegram-ready rendering (text only) that includes: severity, market question/outcome, source fact + timestamp + URL, market observation/time/spread, adjudication conclusion, invalidation, and artifact references. It must say `PAPER ONLY` prominently.

## Seeded directive taxonomy

Implement a versioned card/taxonomy file plus deterministic stratified seed compiler. It should support the following families and cards, but select only eligible cards based on desk constraints:

- `primary_source_sniper`: official statistics release, court/docket/opinion, election authority result, regulator/agency notice, corporate IR/filing;
- `resolution_oracle`: rules text + designated resolution source research only; human-only for UMA propose/dispute;
- `semantic_basis`: deferred unless a cross-venue entailment/compliance capability is explicitly available;
- `wallet_skill`: paper-only, explicitly prohibits blind copy;
- `neg_risk_mm`: deferred until later;
- `explore_seed`: forces unfamiliar but eligible cluster.

Use a reproducible seed created from run date + desk-state hash + optional nonce. Include tests showing same input returns same cards, excluded capabilities remove cards, and output is bounded.

## Testing and quality standard

Use strict TDD: for each vertical behavior, write a focused failing test, run it and observe expected failure, implement minimal code, rerun green. In your final report identify test commands and actual outputs. Do not claim TDD if you did not run tests.

Minimum expectations:

- TypeScript strict mode; lint/format scripts; test runner (Vitest preferred).
- Unit tests for schemas, adapters (fake transport), persistence, artifact hashes, source validation, monitor rules, HMAC/auth/idempotency, dispatcher guard, taxonomy seed reproducibility, ledger invariants, Telegram renderer.
- At least one end-to-end local fixture test:
  `source fixture v1 → source fixture v2 changed → stored evidence → emitted SignalEnvelope → ingress queues it → adjudication fixture output → paper ledger row + rendered alert`.
- Guard test verifies no prohibited trading/wallet/secure-client imports or strings in production code.
- Tests do not access network by default; live integrations are clearly named and opt-in.
- No placeholder `TODO` that substitutes for a deliverable.
- Explicit error types / actionable CLI error messages.
- Never commit `.env`, secrets, database state, Browserbase recordings, or large generated artifacts.

## Deliverable ergonomics

Create a high-quality `README.md` containing:

1. architecture diagram;
2. safe setup (`npm install`, env names only, init DB);
3. test/lint commands;
4. examples for taxonomy compile, market discovery, fixture source collection, monitor evaluate, ingress submission, queued signal inspection, workflow dry run, and paper ledger export;
5. Browserbase opt-in setup and explicit boundaries;
6. Hermes webhook/manual launcher opt-in setup (not executed);
7. clear explanation of paper-only boundary and future live-execution requirements.

Supply fixture data and sample specs that make the entire E2E test runnable offline. Keep code layout intelligible (e.g. `src/polymarket`, `src/store`, `src/monitor`, `src/browserbase`, `src/ingress`, `src/ledger`, `src/taxonomy`, `src/workflow`, `tests`).

## Git/commit discipline

- Start on `feat/pm-desk-mvp`.
- Work in small coherent commits, approximately a few hundred LOC each, with conventional messages:
  - `feat(pm-store): ...`
  - `feat(pm-data): ...`
  - `feat(pm-monitor): ...`
  - `feat(pm-source): ...`
  - `feat(pm-ingress): ...`
  - `feat(pm-ledger): ...`
  - `docs(pm-desk): ...`
- Do not push, create a GitHub repository, or open a PR. User has not asked for remote publication.
- Before finishing: run full tests, lint, TypeScript check/build, and a safe offline E2E demo. Then inspect `git diff main...HEAD`, `git status`, and report only actual results.

## Definition of done

All six components work together locally from fixture data, tests are green, lint/typecheck/build are green, a tiny official-public-SDK live smoke is exercised if network allows, nothing can trade or touch a wallet, and the README provides reproducible safe commands.

When finished, leave a concise `BUILD_SUMMARY.md` covering architecture, completed CLI commands, actual test outputs, live-smoke result, limitations, and exact changes. Do not say the work is complete if any required component remains missing.
