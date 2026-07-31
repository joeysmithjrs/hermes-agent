import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { contentHash } from '../src/core/hash.js';
import { fixedClock } from '../src/core/time.js';
import { diffExcerpt } from '../src/monitor/diff.js';
import { evaluateMonitor } from '../src/monitor/engine.js';
import { parseMonitorSpec } from '../src/schema/monitor-spec.js';
import { parseSignalEnvelope } from '../src/schema/signal.js';
import { openStore, type DeskStore } from '../src/store/index.js';

let dir: string;
let store: DeskStore;

const NOW = '2026-07-30T12:00:00.000Z';

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'pm-desk-mon-'));
  store = openStore({ home: dir });
});

afterEach(() => {
  store.close();
  rmSync(dir, { recursive: true, force: true });
});

function recordSnapshot(text: string, at: string, sourceId = 'example_official_release') {
  return store.sources.record({
    source_id: sourceId,
    spec_version: 1,
    url: 'https://example.gov/release',
    collected_at: at,
    normalized_text: text,
    content_hash: contentHash(text),
    raw_artifact_ref: store.artifacts.put(`<html>${text}</html>`, 'text/html').ref,
    fields: { title: 'Release' },
  });
}

function seedMarket() {
  store.markets.upsertMarket({
    market_id: 'm1',
    condition_id: '0xcond',
    question: 'Will Q1 GDP be revised below 3.0%?',
    active: true,
    closed: false,
    accepting_orders: true,
    neg_risk: false,
    end_date: '2026-08-05T12:00:00.000Z',
    observed_at: NOW,
    raw: {},
  });
  store.markets.upsertToken({
    token_id: 'tok1',
    market_id: 'm1',
    outcome: 'YES',
    label: 'Yes',
    observed_at: NOW,
  });
}

const sourceSpec = (overrides: Record<string, unknown> = {}) =>
  parseMonitorSpec({
    id: 'gdp_release_change',
    version: 1,
    kind: 'primary_source_change',
    severity: 'high',
    cooldown_s: 3600,
    params: { source_id: 'example_official_release', ...(overrides.params ?? {}) },
    ...overrides,
  });

const moveSpec = (params: Record<string, unknown> = {}) =>
  parseMonitorSpec({
    id: 'mid_move',
    version: 1,
    kind: 'market_move',
    severity: 'warn',
    cooldown_s: 900,
    params: {
      token_id: 'tok1',
      market_id: 'm1',
      outcome: 'YES',
      abs_move: 0.05,
      lookback_s: 3600,
      max_staleness_s: 900,
      ...params,
    },
  });

describe('diff excerpt', () => {
  it('shows the changed region with surrounding context', () => {
    const excerpt = diffExcerpt(
      'GDP increased at an annual rate of 3.1 percent in the first quarter.',
      'GDP increased at an annual rate of 2.4 percent in the first quarter.',
    );
    expect(excerpt).toContain('3.1');
    expect(excerpt).toContain('2.4');
    expect(excerpt).toMatch(/^- /m);
    expect(excerpt).toMatch(/^\+ /m);
  });

  it('handles a first-ever snapshot with no previous text', () => {
    expect(diffExcerpt(null, 'brand new content')).toContain('brand new content');
  });

  it('is bounded so a huge document cannot blow up an envelope', () => {
    const excerpt = diffExcerpt('a '.repeat(50_000), 'b '.repeat(50_000), 400);
    expect(excerpt.length).toBeLessThanOrEqual(500);
  });
});

describe('primary_source_change rule', () => {
  it('is SILENT when the source has not changed', () => {
    recordSnapshot('Q1 GDP: 3.1%', '2026-07-30T11:00:00.000Z');
    recordSnapshot('Q1 GDP: 3.1%', '2026-07-30T11:55:00.000Z');

    const result = evaluateMonitor(store, sourceSpec(), { clock: fixedClock(NOW) });
    expect(result.outcome).toBe('silent');
    expect(result.signal).toBeUndefined();
  });

  it('emits a schema-valid envelope when the fingerprint changes', () => {
    recordSnapshot('Q1 GDP: 3.1%', '2026-07-30T11:00:00.000Z');
    const current = recordSnapshot('Q1 GDP: 2.4%', '2026-07-30T11:55:00.000Z');

    const result = evaluateMonitor(store, sourceSpec(), { clock: fixedClock(NOW) });
    expect(result.outcome).toBe('emitted');

    const signal = parseSignalEnvelope(result.signal);
    expect(signal.kind).toBe('primary_source_change');
    expect(signal.severity).toBe('high');
    expect(signal.paper_only).toBe(true);
    expect(signal.rule_id).toBe('gdp_release_change');
    expect(signal.source_refs[0]?.current_hash).toBe(current.content_hash);
    expect(signal.source_refs[0]?.previous_hash).toBe(contentHash('Q1 GDP: 3.1%'));
    expect(signal.source_refs[0]?.artifact_ref).toBe(current.normalized_artifact_ref);
    expect(signal.evidence.diff_excerpt).toContain('2.4');
  });

  it('reports missing data rather than emitting when no snapshot exists', () => {
    const result = evaluateMonitor(store, sourceSpec(), { clock: fixedClock(NOW) });
    expect(result.outcome).toBe('skipped_missing_data');
    expect(result.reason).toMatch(/no snapshot/i);
  });

  it('refuses to alert on stale evidence', () => {
    recordSnapshot('old', '2026-07-01T00:00:00.000Z');
    recordSnapshot('older changed', '2026-07-02T00:00:00.000Z');

    const result = evaluateMonitor(
      store,
      sourceSpec({ params: { source_id: 'example_official_release', max_staleness_s: 3600 } }),
      { clock: fixedClock(NOW) },
    );
    expect(result.outcome).toBe('skipped_stale');
  });

  it('ignores a change smaller than min_diff_chars', () => {
    recordSnapshot('Q1 GDP: 3.1%', '2026-07-30T11:00:00.000Z');
    recordSnapshot('Q1 GDP: 3.1%.', '2026-07-30T11:55:00.000Z');

    const result = evaluateMonitor(
      store,
      sourceSpec({ params: { source_id: 'example_official_release', min_diff_chars: 20 } }),
      { clock: fixedClock(NOW) },
    );
    expect(result.outcome).toBe('silent');
  });
});

describe('dedupe and cooldown', () => {
  beforeEach(() => {
    recordSnapshot('Q1 GDP: 3.1%', '2026-07-30T11:00:00.000Z');
    recordSnapshot('Q1 GDP: 2.4%', '2026-07-30T11:55:00.000Z');
  });

  it('emits once, then suppresses the identical signal as a duplicate', () => {
    const first = evaluateMonitor(store, sourceSpec(), { clock: fixedClock(NOW) });
    expect(first.outcome).toBe('emitted');

    const second = evaluateMonitor(store, sourceSpec(), {
      clock: fixedClock('2026-07-30T20:00:00.000Z'), // well past cooldown
    });
    expect(second.outcome).toBe('suppressed_duplicate');
    expect(store.signals.count()).toBe(1);
  });

  it('suppresses a genuinely new signal that arrives inside the cooldown window', () => {
    expect(evaluateMonitor(store, sourceSpec(), { clock: fixedClock(NOW) }).outcome).toBe(
      'emitted',
    );
    recordSnapshot('Q1 GDP: 2.9%', '2026-07-30T12:10:00.000Z');

    const result = evaluateMonitor(store, sourceSpec(), {
      clock: fixedClock('2026-07-30T12:15:00.000Z'), // 15 min < 3600s cooldown
    });
    expect(result.outcome).toBe('suppressed_cooldown');
    expect(store.signals.count()).toBe(1);
  });

  it('emits again once the cooldown has elapsed and the content is new', () => {
    evaluateMonitor(store, sourceSpec(), { clock: fixedClock(NOW) });
    recordSnapshot('Q1 GDP: 2.9%', '2026-07-30T13:10:00.000Z');

    const result = evaluateMonitor(store, sourceSpec(), {
      clock: fixedClock('2026-07-30T13:15:00.000Z'),
    });
    expect(result.outcome).toBe('emitted');
    expect(store.signals.count()).toBe(2);
  });

  it('a dry run is repeatable: identical output, no state written', () => {
    const opts = { clock: fixedClock(NOW), dryRun: true };
    const a = evaluateMonitor(store, sourceSpec(), opts);
    const b = evaluateMonitor(store, sourceSpec(), opts);

    expect(a.outcome).toBe('emitted');
    expect(JSON.stringify(a.signal)).toBe(JSON.stringify(b.signal));
    expect(store.signals.count()).toBe(0);
    expect(store.monitorState.listDecisions()).toHaveLength(0);
  });

  it('records an audit decision for every evaluation, including silent ones', () => {
    evaluateMonitor(store, sourceSpec(), { clock: fixedClock(NOW) });
    evaluateMonitor(store, sourceSpec(), { clock: fixedClock('2026-07-30T20:00:00.000Z') });
    expect(store.monitorState.listDecisions('gdp_release_change').map((d) => d.outcome)).toEqual([
      'emitted',
      'suppressed_duplicate',
    ]);
  });

  it('skips a disabled rule without touching anything', () => {
    const result = evaluateMonitor(store, sourceSpec({ enabled: false }), {
      clock: fixedClock(NOW),
    });
    expect(result.outcome).toBe('skipped_disabled');
    expect(store.signals.count()).toBe(0);
  });
});

describe('market_move rule', () => {
  beforeEach(seedMarket);

  const obs = (at: string, mid: number | null, extra: Record<string, unknown> = {}) =>
    store.observations.append({
      token_id: 'tok1',
      market_id: 'm1',
      observed_at: at,
      mid,
      best_bid: mid === null ? null : mid - 0.005,
      best_ask: mid === null ? null : mid + 0.005,
      spread: mid === null ? null : 0.01,
      book_available: mid !== null,
      ...extra,
    });

  it('is SILENT when the move is below the absolute threshold', () => {
    obs('2026-07-30T11:00:00.000Z', 0.5);
    obs('2026-07-30T11:58:00.000Z', 0.52);
    expect(evaluateMonitor(store, moveSpec(), { clock: fixedClock(NOW) }).outcome).toBe('silent');
  });

  it('emits when the absolute move meets the threshold', () => {
    obs('2026-07-30T10:55:00.000Z', 0.5);
    obs('2026-07-30T11:58:00.000Z', 0.61);

    const result = evaluateMonitor(store, moveSpec(), { clock: fixedClock(NOW) });
    expect(result.outcome).toBe('emitted');

    const signal = parseSignalEnvelope(result.signal);
    expect(signal.kind).toBe('market_move');
    expect(signal.market_refs[0]).toMatchObject({
      market_id: 'm1',
      token_id: 'tok1',
      outcome: 'YES',
      condition_id: '0xcond',
    });
    expect(signal.market_snapshot?.mid).toBe(0.61);
    expect(signal.evidence.metrics?.abs_move).toBeCloseTo(0.11, 6);
  });

  it('fires on a relative move that is below the absolute threshold', () => {
    obs('2026-07-30T10:55:00.000Z', 0.04);
    obs('2026-07-30T11:58:00.000Z', 0.08);

    const spec = moveSpec({ abs_move: undefined, rel_move: 0.5 });
    const result = evaluateMonitor(store, spec, { clock: fixedClock(NOW) });
    expect(result.outcome).toBe('emitted');
    expect(parseSignalEnvelope(result.signal).evidence.metrics?.rel_move).toBeCloseTo(1.0, 6);
  });

  it('refuses to compare against a stale latest observation', () => {
    obs('2026-07-30T09:00:00.000Z', 0.5);
    obs('2026-07-30T10:00:00.000Z', 0.9); // 2h old, max_staleness_s is 900
    expect(evaluateMonitor(store, moveSpec(), { clock: fixedClock(NOW) }).outcome).toBe(
      'skipped_stale',
    );
  });

  it('reports missing data when the latest observation has no midpoint', () => {
    obs('2026-07-30T10:55:00.000Z', 0.5);
    obs('2026-07-30T11:58:00.000Z', null);
    expect(evaluateMonitor(store, moveSpec(), { clock: fixedClock(NOW) }).outcome).toBe(
      'skipped_missing_data',
    );
  });

  it('reports missing data when there is no observation before the lookback boundary', () => {
    obs('2026-07-30T11:58:00.000Z', 0.61);
    expect(evaluateMonitor(store, moveSpec(), { clock: fixedClock(NOW) }).outcome).toBe(
      'skipped_missing_data',
    );
  });

  it('does not re-emit for the same observation pair', () => {
    obs('2026-07-30T10:55:00.000Z', 0.5);
    obs('2026-07-30T11:58:00.000Z', 0.61);
    expect(evaluateMonitor(store, moveSpec(), { clock: fixedClock(NOW) }).outcome).toBe('emitted');
    // Still inside max_staleness_s, so this exercises dedupe rather than staleness.
    expect(
      evaluateMonitor(store, moveSpec(), { clock: fixedClock('2026-07-30T12:10:00.000Z') }).outcome,
    ).toBe('suppressed_duplicate');
  });
});

describe('spread_widening rule', () => {
  beforeEach(seedMarket);

  const spreadSpec = (params: Record<string, unknown> = {}) =>
    parseMonitorSpec({
      id: 'book_health',
      version: 1,
      kind: 'spread_widening',
      severity: 'warn',
      cooldown_s: 600,
      params: { token_id: 'tok1', max_spread: 0.05, max_staleness_s: 900, ...params },
    });

  it('is SILENT while the book is tight', () => {
    store.observations.append({
      token_id: 'tok1',
      observed_at: '2026-07-30T11:58:00.000Z',
      mid: 0.5,
      best_bid: 0.495,
      best_ask: 0.505,
      spread: 0.01,
      book_available: true,
    });
    expect(evaluateMonitor(store, spreadSpec(), { clock: fixedClock(NOW) }).outcome).toBe('silent');
  });

  it('emits when the spread reaches the configured width', () => {
    store.observations.append({
      token_id: 'tok1',
      observed_at: '2026-07-30T11:58:00.000Z',
      mid: 0.5,
      best_bid: 0.45,
      best_ask: 0.56,
      spread: 0.11,
      book_available: true,
    });
    const result = evaluateMonitor(store, spreadSpec(), { clock: fixedClock(NOW) });
    expect(result.outcome).toBe('emitted');
    expect(parseSignalEnvelope(result.signal).evidence.metrics?.spread).toBeCloseTo(0.11, 6);
  });

  it('emits a data-quality signal when the book is missing entirely', () => {
    store.observations.append({
      token_id: 'tok1',
      observed_at: '2026-07-30T11:58:00.000Z',
      mid: null,
      spread: null,
      book_available: false,
    });
    const result = evaluateMonitor(store, spreadSpec(), { clock: fixedClock(NOW) });
    expect(result.outcome).toBe('emitted');
    expect(parseSignalEnvelope(result.signal).evidence.claims.join(' ')).toMatch(/book/i);
  });

  it('stays silent on a missing book when that alarm is switched off', () => {
    store.observations.append({
      token_id: 'tok1',
      observed_at: '2026-07-30T11:58:00.000Z',
      mid: null,
      spread: null,
      book_available: false,
    });
    const result = evaluateMonitor(store, spreadSpec({ alert_on_missing_book: false }), {
      clock: fixedClock(NOW),
    });
    expect(result.outcome).toBe('silent');
  });
});

describe('source_market_divergence rule', () => {
  const divergenceSpec = (params: Record<string, unknown> = {}) =>
    parseMonitorSpec({
      id: 'gdp_vs_market',
      version: 1,
      kind: 'source_market_divergence',
      severity: 'critical',
      cooldown_s: 1800,
      params: {
        source_id: 'example_official_release',
        market: { token_id: 'tok1', market_id: 'm1', outcome: 'YES' },
        horizon_s: 86_400 * 7,
        require_active: true,
        max_staleness_s: 3600,
        ...params,
      },
    });

  beforeEach(() => {
    seedMarket();
    recordSnapshot('Q1 GDP: 3.1%', '2026-07-30T11:00:00.000Z');
  });

  it('emits with both source and market provenance when a linked source changes', () => {
    recordSnapshot('Q1 GDP: 2.4%', '2026-07-30T11:55:00.000Z');
    store.observations.append({
      token_id: 'tok1',
      market_id: 'm1',
      observed_at: '2026-07-30T11:58:00.000Z',
      mid: 0.32,
      best_bid: 0.31,
      best_ask: 0.33,
      spread: 0.02,
      book_available: true,
    });

    const result = evaluateMonitor(store, divergenceSpec(), { clock: fixedClock(NOW) });
    expect(result.outcome).toBe('emitted');

    const signal = parseSignalEnvelope(result.signal);
    expect(signal.kind).toBe('source_market_divergence');
    expect(signal.source_refs).toHaveLength(1);
    expect(signal.market_refs[0]?.token_id).toBe('tok1');
    expect(signal.market_snapshot?.mid).toBe(0.32);
  });

  it('is SILENT when the source has not changed', () => {
    recordSnapshot('Q1 GDP: 3.1%', '2026-07-30T11:55:00.000Z');
    expect(evaluateMonitor(store, divergenceSpec(), { clock: fixedClock(NOW) }).outcome).toBe(
      'silent',
    );
  });

  it('is SILENT when the linked market has already closed', () => {
    recordSnapshot('Q1 GDP: 2.4%', '2026-07-30T11:55:00.000Z');
    store.markets.upsertMarket({
      market_id: 'm1',
      condition_id: '0xcond',
      question: 'Will Q1 GDP be revised below 3.0%?',
      active: false,
      closed: true,
      accepting_orders: false,
      neg_risk: false,
      end_date: '2026-08-05T12:00:00.000Z',
      observed_at: NOW,
      raw: {},
    });
    const result = evaluateMonitor(store, divergenceSpec(), { clock: fixedClock(NOW) });
    expect(result.outcome).toBe('silent');
    expect(result.reason).toMatch(/closed|not active/i);
  });

  it('is SILENT when the market resolves beyond the configured horizon', () => {
    recordSnapshot('Q1 GDP: 2.4%', '2026-07-30T11:55:00.000Z');
    const result = evaluateMonitor(store, divergenceSpec({ horizon_s: 3600 }), {
      clock: fixedClock(NOW),
    });
    expect(result.outcome).toBe('silent');
    expect(result.reason).toMatch(/horizon/i);
  });

  it('emits without a market snapshot when no observation has been taken yet', () => {
    recordSnapshot('Q1 GDP: 2.4%', '2026-07-30T11:55:00.000Z');
    const result = evaluateMonitor(store, divergenceSpec(), { clock: fixedClock(NOW) });
    expect(result.outcome).toBe('emitted');
    expect(parseSignalEnvelope(result.signal).market_snapshot).toBeNull();
  });
});
