import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { collectSource } from '../src/browserbase/collector.js';
import { FixtureBrowser } from '../src/browserbase/fixture-browser.js';
import { loadSourceSpec } from '../src/browserbase/spec-loader.js';
import { submitSignal } from '../src/ingress/client.js';
import { OutboxDispatcher } from '../src/ingress/dispatcher.js';
import { startIngressServer, type IngressServer } from '../src/ingress/server.js';
import { recordFromAdjudication } from '../src/ledger/ledger.js';
import { renderTelegramAlert } from '../src/ledger/render.js';
import { evaluateMonitor } from '../src/monitor/engine.js';
import { collectMarkets, snapshotTokens } from '../src/polymarket/collector.js';
import { parseAdjudication } from '../src/schema/adjudication.js';
import { parseMonitorSpec } from '../src/schema/monitor-spec.js';
import { parseSignalEnvelope } from '../src/schema/signal.js';
import { openStore, type DeskStore } from '../src/store/index.js';
import { renderAdjudicationPrompt } from '../src/workflow/prompt.js';
import { FakePublicClient } from './helpers/fake-polymarket.js';

/**
 * The whole desk loop, offline.
 *
 *   market discovery (fake SDK client)
 *     → source fixture v1  → stored evidence
 *     → source fixture v2  → detected content change
 *     → deterministic monitor → SignalEnvelope
 *     → local HMAC ingress   → recorded + queued
 *     → adjudication fixture → paper_alert
 *     → paper ledger row     → rendered PAPER ONLY alert
 *
 * No network, no credentials, no LLM. Everything a reviewer needs to run this
 * is checked into the repository.
 */

const ROOT = join(import.meta.dirname, '..');
const SPEC_PATH = join(ROOT, 'specs', 'sources', 'example_official_release.yaml');
const FIXTURE_V1 = join(ROOT, 'fixtures', 'sources', 'example_official_release.v1.html');
const FIXTURE_V2 = join(ROOT, 'fixtures', 'sources', 'example_official_release.v2.html');
const ADJUDICATION_FIXTURE = join(ROOT, 'fixtures', 'adjudication', 'paper_alert.json');

const SECRET = 'e'.repeat(64);

let dir: string;
let store: DeskStore;
let server: IngressServer;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'pm-desk-e2e-'));
  store = openStore({ home: dir });
});

afterEach(async () => {
  if (server) await server.close();
  store.close();
  rmSync(dir, { recursive: true, force: true });
});

describe('end-to-end: source change → signal → ingress → adjudication → paper ledger', () => {
  it('carries evidence all the way to a rendered PAPER ONLY alert', async () => {
    // ---- 1. Market discovery and a baseline observation --------------------
    const client = new FakePublicClient({ marketCount: 1 });
    const discovery = await collectMarkets(store, client, { limit: 1 });
    expect(discovery.markets).toBe(1);

    const tokenId = 'tokenYES-0';
    await snapshotTokens(store, client, [tokenId]);
    const observation = store.observations.latest(tokenId);
    expect(observation?.mid).toBe(0.505);

    // ---- 2. Collect the source at v1 --------------------------------------
    const spec = loadSourceSpec(SPEC_PATH);
    const first = await collectSource(store, spec, { browser: new FixtureBrowser(FIXTURE_V1) });
    expect(first.changed).toBe(true);
    expect(first.fields.title).toContain('Advance Estimate');

    // ---- 3. Collect again at v2: the figure has been revised ---------------
    const second = await collectSource(store, spec, { browser: new FixtureBrowser(FIXTURE_V2) });
    expect(second.changed).toBe(true);
    expect(second.previous_hash).toBe(first.content_hash);
    expect(second.fields.title).toContain('Second Estimate');

    // Evidence is content-addressed and re-verifiable.
    expect(store.artifacts.verify(second.normalized_artifact_ref!)).toBe(true);
    expect(store.artifacts.read(second.normalized_artifact_ref!)).toContain('2.4 percent');

    // ---- 4. The deterministic monitor emits a signal -----------------------
    const monitorSpec = parseMonitorSpec({
      id: 'gdp_release_vs_market',
      version: 1,
      kind: 'source_market_divergence',
      severity: 'high',
      cooldown_s: 3600,
      params: {
        source_id: spec.id,
        market: { token_id: tokenId, market_id: 'market-0', outcome: 'YES' },
        horizon_s: 86_400 * 7,
        require_active: true,
        max_staleness_s: 3600,
      },
    });

    const evaluation = evaluateMonitor(store, monitorSpec);
    expect(evaluation.outcome).toBe('emitted');

    const signal = parseSignalEnvelope(evaluation.signal);
    expect(signal.paper_only).toBe(true);
    expect(signal.kind).toBe('source_market_divergence');
    expect(signal.source_refs[0]?.current_hash).toBe(second.content_hash);
    expect(signal.source_refs[0]?.previous_hash).toBe(first.content_hash);
    expect(signal.market_refs[0]?.token_id).toBe(tokenId);
    expect(signal.market_snapshot?.mid).toBe(0.505);
    expect(signal.evidence.diff_excerpt).toMatch(/2\.4 percent/);

    // Re-evaluating the same facts is a duplicate, not a second alert.
    expect(evaluateMonitor(store, monitorSpec).outcome).toBe('suppressed_duplicate');

    // ---- 5. The signal reaches the local ingress and is queued -------------
    server = await startIngressServer({
      store,
      secret: SECRET,
      dispatcher: new OutboxDispatcher(store),
      host: '127.0.0.1',
      port: 0,
    });

    const submitted = await submitSignal(signal, { url: server.url, secret: SECRET });
    expect(submitted.status).toBe(202);
    expect(submitted.body).toMatchObject({ status: 'accepted', dispatched: true });

    const queued = store.signals.listOutbox('queued');
    expect(queued).toHaveLength(1);
    expect(queued[0]?.signal_id).toBe(signal.signal_id);

    // The queued artifact is the exact envelope, ready for handoff.
    const queuedEnvelope = parseSignalEnvelope(
      JSON.parse(store.artifacts.read(queued[0]!.artifact_ref!)),
    );
    expect(queuedEnvelope.signal_id).toBe(signal.signal_id);

    // A replay changes nothing.
    const replay = await submitSignal(signal, { url: server.url, secret: SECRET });
    expect(replay.body).toMatchObject({ status: 'duplicate' });
    expect(store.signals.listOutbox()).toHaveLength(1);

    // ---- 6. The adjudication prompt is renderable from stored evidence -----
    const prompt = renderAdjudicationPrompt(store, signal);
    expect(prompt).toContain('PAPER ONLY');
    expect(prompt).toContain('2.4 percent');
    expect(prompt).toContain(signal.signal_id);
    expect(prompt).toContain('ignore');
    expect(prompt).toContain('paper_alert');

    // ---- 7. Adjudication fixture stands in for the workflow's output -------
    const fixture = JSON.parse(readFileSync(ADJUDICATION_FIXTURE, 'utf8')) as Record<
      string,
      unknown
    >;
    delete fixture._comment;
    const adjudication = parseAdjudication({ ...fixture, signal_id: signal.signal_id });
    expect(adjudication.decision).toBe('paper_alert');

    // ---- 8. Paper ledger row -----------------------------------------------
    const entry = recordFromAdjudication(store, adjudication);
    expect(entry.signal_id).toBe(signal.signal_id);
    expect(entry.origin).toBe('adjudication');
    expect(entry.fill_type).toBe('SIMULATED_NO_FILL');
    expect(entry.paper_only).toBe(true);
    expect(entry.outcome).toBe('YES');
    // cross_spread_full on a YES buy pays the recorded ask.
    expect(entry.assumed_entry_price).toBe(0.51);
    expect(entry.entry_observed_at).toBe(signal.market_snapshot?.observed_at);
    expect(entry.evidence_refs).toContain(second.normalized_artifact_ref);
    expect(entry.evidence_refs).toContain(spec.url);

    // The adjudication itself is recorded alongside the entry.
    expect(store.adjudications.get(signal.signal_id)?.decision).toBe('paper_alert');

    // ---- 9. Rendered operator alert ----------------------------------------
    const alert = renderTelegramAlert({ signal, adjudication, entry });
    expect(alert.split('\n')[0]).toContain('PAPER ONLY');
    expect(alert).toContain('HIGH');
    expect(alert).toContain('https://example.gov/release');
    expect(alert).toContain(entry.entry_id);
    expect(alert).toContain('SIMULATED_NO_FILL');
    expect(alert).toContain('sha256:');
    expect(alert.length).toBeLessThanOrEqual(4096);

    // ---- 10. Nothing anywhere claims a real fill ---------------------------
    expect(store.ledger.count()).toBe(1);
    const rows = store.db.prepare('SELECT paper_only, fill_type FROM paper_ledger').all();
    expect(rows).toEqual([{ paper_only: 1, fill_type: 'SIMULATED_NO_FILL' }]);
  }, 30_000);

  it('stays silent end-to-end when the source has not changed', async () => {
    const client = new FakePublicClient({ marketCount: 1 });
    await collectMarkets(store, client, { limit: 1 });
    await snapshotTokens(store, client, ['tokenYES-0']);

    const spec = loadSourceSpec(SPEC_PATH);
    await collectSource(store, spec, { browser: new FixtureBrowser(FIXTURE_V1) });
    await collectSource(store, spec, { browser: new FixtureBrowser(FIXTURE_V1) });

    const monitorSpec = parseMonitorSpec({
      id: 'gdp_release_vs_market',
      version: 1,
      kind: 'source_market_divergence',
      severity: 'high',
      cooldown_s: 3600,
      params: {
        source_id: spec.id,
        market: { token_id: 'tokenYES-0', market_id: 'market-0', outcome: 'YES' },
        max_staleness_s: 3600,
      },
    });

    const evaluation = evaluateMonitor(store, monitorSpec);
    expect(evaluation.outcome).toBe('silent');
    expect(evaluation.signal).toBeUndefined();
    expect(store.signals.count()).toBe(0);
    expect(store.ledger.count()).toBe(0);
  }, 30_000);
});
