import { mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { StoreError } from '../src/core/errors.js';
import { contentHash } from '../src/core/hash.js';
import { openStore, type DeskStore } from '../src/store/index.js';

let dir: string;
let store: DeskStore;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'pm-desk-store-'));
  store = openStore({ home: dir });
});

afterEach(() => {
  store.close();
  rmSync(dir, { recursive: true, force: true });
});

describe('migrations', () => {
  it('creates the schema and reports the applied version', () => {
    expect(store.schemaVersion()).toBeGreaterThan(0);
  });

  it('is idempotent — reopening the same home does not re-apply or lose data', () => {
    store.markets.upsertMarket({
      market_id: '540817',
      condition_id: '0xcond',
      question: 'New Rihanna Album before GTA VI?',
      slug: 'new-rhianna-album-before-gta-vi-926',
      active: true,
      closed: false,
      accepting_orders: true,
      neg_risk: false,
      end_date: '2026-07-31T12:00:00.000Z',
      observed_at: '2026-07-30T12:00:00.000Z',
      raw: { id: '540817' },
    });
    const version = store.schemaVersion();
    store.close();

    store = openStore({ home: dir });
    expect(store.schemaVersion()).toBe(version);
    expect(store.markets.getMarket('540817')?.question).toBe('New Rihanna Album before GTA VI?');
  });
});

describe('market repository', () => {
  const market = {
    market_id: '540817',
    condition_id: '0xcond',
    question: 'Q?',
    slug: 'q',
    active: true,
    closed: false,
    accepting_orders: true,
    neg_risk: false,
    end_date: '2026-07-31T12:00:00.000Z',
    observed_at: '2026-07-30T12:00:00.000Z',
    raw: {},
  };

  it('upserts markets and outcome tokens, then reads them back normalized', () => {
    store.markets.upsertMarket(market);
    store.markets.upsertToken({
      token_id: '9802',
      market_id: '540817',
      outcome: 'YES',
      label: 'Yes',
      observed_at: market.observed_at,
    });
    store.markets.upsertToken({
      token_id: '5383',
      market_id: '540817',
      outcome: 'NO',
      label: 'No',
      observed_at: market.observed_at,
    });

    const tokens = store.markets.listTokens('540817');
    expect(tokens.map((t) => t.outcome).sort()).toEqual(['NO', 'YES']);
    expect(store.markets.getTokenBinding('9802')).toMatchObject({
      market_id: '540817',
      condition_id: '0xcond',
      outcome: 'YES',
    });
  });

  it('upsert is idempotent on market_id and updates mutable state', () => {
    store.markets.upsertMarket(market);
    store.markets.upsertMarket({
      ...market,
      closed: true,
      observed_at: '2026-07-30T13:00:00.000Z',
    });
    expect(store.markets.countMarkets()).toBe(1);
    expect(store.markets.getMarket('540817')?.closed).toBe(true);
  });
});

describe('observation repository', () => {
  const base = { token_id: '9802', market_id: '540817' };

  it('appends observations and returns the latest by observed_at', () => {
    store.observations.append({ ...base, observed_at: '2026-07-30T12:00:00.000Z', mid: 0.5 });
    store.observations.append({ ...base, observed_at: '2026-07-30T12:30:00.000Z', mid: 0.61 });
    store.observations.append({ ...base, observed_at: '2026-07-30T12:15:00.000Z', mid: 0.55 });

    expect(store.observations.latest('9802')?.mid).toBe(0.61);
    expect(store.observations.count('9802')).toBe(3);
  });

  it('returns the reference observation at or before a lookback boundary', () => {
    store.observations.append({ ...base, observed_at: '2026-07-30T11:00:00.000Z', mid: 0.4 });
    store.observations.append({ ...base, observed_at: '2026-07-30T11:50:00.000Z', mid: 0.5 });
    store.observations.append({ ...base, observed_at: '2026-07-30T12:00:00.000Z', mid: 0.61 });

    // Lookback boundary 12:00 minus 30min = 11:30 -> most recent at/before is 11:00.
    const ref = store.observations.latestAtOrBefore('9802', '2026-07-30T11:30:00.000Z');
    expect(ref?.mid).toBe(0.4);
    expect(store.observations.latestAtOrBefore('9802', '2026-07-30T10:00:00.000Z')).toBeUndefined();
  });

  it('persists a missing book as null rather than zero', () => {
    store.observations.append({
      ...base,
      observed_at: '2026-07-30T12:00:00.000Z',
      mid: null,
      best_bid: null,
      best_ask: null,
      spread: null,
      book_available: false,
    });
    const latest = store.observations.latest('9802');
    expect(latest?.mid).toBeNull();
    expect(latest?.book_available).toBe(false);
  });
});

describe('artifact store', () => {
  it('is content addressed: identical bytes produce one file and one ref', () => {
    const a = store.artifacts.put('hello world', 'text/plain');
    const b = store.artifacts.put('hello world', 'text/plain');
    expect(a.ref).toBe(b.ref);
    expect(a.ref).toMatch(/^sha256:[0-9a-f]{64}$/);
    expect(store.artifacts.count()).toBe(1);
    expect(readFileSync(a.path, 'utf8')).toBe('hello world');
  });

  it('reads content back through the ref and reports unknown refs', () => {
    const { ref } = store.artifacts.put('payload', 'text/plain');
    expect(store.artifacts.read(ref)).toBe('payload');
    expect(() => store.artifacts.read('sha256:' + 'f'.repeat(64))).toThrow(StoreError);
  });
});

describe('source snapshot repository', () => {
  const snapshot = (text: string, at: string) => ({
    source_id: 'example_official_release',
    spec_version: 1,
    url: 'https://example.gov/release',
    collected_at: at,
    normalized_text: text,
    content_hash: contentHash(text),
    raw_artifact_ref: store.artifacts.put(`<html>${text}</html>`, 'text/html').ref,
    fields: { title: 'Release' },
  });

  it('records a snapshot, stores its normalized body in the CAS and links provenance', () => {
    const rec = store.sources.record(snapshot('Q1 GDP: 3.1%', '2026-07-30T12:00:00.000Z'));
    expect(rec.changed).toBe(true); // first observation is a change from nothing
    expect(rec.previous_hash).toBeNull();
    expect(store.artifacts.read(rec.normalized_artifact_ref)).toBe('Q1 GDP: 3.1%');
  });

  it('detects an unchanged re-collection without creating duplicate artifacts', () => {
    store.sources.record(snapshot('Q1 GDP: 3.1%', '2026-07-30T12:00:00.000Z'));
    const before = store.artifacts.count();
    const second = store.sources.record(snapshot('Q1 GDP: 3.1%', '2026-07-30T12:05:00.000Z'));
    expect(second.changed).toBe(false);
    expect(second.previous_hash).toBe(second.content_hash);
    expect(store.artifacts.count()).toBe(before);
  });

  it('detects a real content change and exposes the prior snapshot for diffing', () => {
    const first = store.sources.record(snapshot('Q1 GDP: 3.1%', '2026-07-30T12:00:00.000Z'));
    const second = store.sources.record(snapshot('Q1 GDP: 2.4%', '2026-07-30T12:05:00.000Z'));
    expect(second.changed).toBe(true);
    expect(second.previous_hash).toBe(first.content_hash);

    const prior = store.sources.previousChanged('example_official_release');
    expect(prior && store.artifacts.read(prior.normalized_artifact_ref)).toBe('Q1 GDP: 3.1%');
    expect(store.sources.latest('example_official_release')?.content_hash).toBe(
      second.content_hash,
    );
  });
});

describe('signal repository', () => {
  const envelope = {
    version: 1 as const,
    signal_id: 'sig_' + 'a'.repeat(32),
    kind: 'primary_source_change' as const,
    severity: 'high' as const,
    observed_at: '2026-07-30T12:00:00.000Z',
    rule_id: 'gdp_release_change',
    rule_version: '1',
    market_refs: [],
    source_refs: [
      {
        source_id: 'example_official_release',
        url: 'https://example.gov/release',
        current_hash: 'b'.repeat(64),
        artifact_ref: 'sha256:' + 'b'.repeat(64),
      },
    ],
    evidence: { claims: ['changed'] },
    paper_only: true as const,
    dedupe_key: 'gdp:1:' + 'b'.repeat(64),
  };

  it('records a signal once and reports duplicates instead of overwriting', () => {
    expect(store.signals.record(envelope, 'monitor').inserted).toBe(true);
    const again = store.signals.record(envelope, 'ingress');
    expect(again.inserted).toBe(false);
    expect(store.signals.count()).toBe(1);
    expect(store.signals.get(envelope.signal_id)?.envelope.rule_id).toBe('gdp_release_change');
  });

  it('treats a distinct signal_id with the same dedupe_key as a duplicate', () => {
    store.signals.record(envelope, 'monitor');
    const twin = { ...envelope, signal_id: 'sig_' + 'c'.repeat(32) };
    expect(store.signals.record(twin, 'ingress').inserted).toBe(false);
    expect(store.signals.count()).toBe(1);
  });

  it('rejects an envelope that fails schema validation at the store boundary', () => {
    expect(() =>
      store.signals.record({ ...envelope, paper_only: false } as never, 'monitor'),
    ).toThrow();
  });
});

describe('monitor state repository', () => {
  it('round-trips cooldown state per rule and dedupe key', () => {
    expect(store.monitorState.get('rule_a', 'key1')).toBeUndefined();
    store.monitorState.markEmitted('rule_a', 'key1', '2026-07-30T12:00:00.000Z');
    expect(store.monitorState.get('rule_a', 'key1')?.last_emitted_at).toBe(
      '2026-07-30T12:00:00.000Z',
    );
    // Different rule with the same key is independent state.
    expect(store.monitorState.get('rule_b', 'key1')).toBeUndefined();

    store.monitorState.markEmitted('rule_a', 'key1', '2026-07-30T13:00:00.000Z');
    expect(store.monitorState.get('rule_a', 'key1')?.last_emitted_at).toBe(
      '2026-07-30T13:00:00.000Z',
    );
  });

  it('records every evaluation outcome for audit, including silent ones', () => {
    store.monitorState.recordDecision({
      rule_id: 'rule_a',
      rule_version: '1',
      evaluated_at: '2026-07-30T12:00:00.000Z',
      outcome: 'silent',
      reason: 'no content change',
    });
    store.monitorState.recordDecision({
      rule_id: 'rule_a',
      rule_version: '1',
      evaluated_at: '2026-07-30T12:05:00.000Z',
      outcome: 'emitted',
      dedupe_key: 'k',
      signal_id: 'sig_' + 'a'.repeat(32),
    });
    const decisions = store.monitorState.listDecisions('rule_a');
    expect(decisions.map((d) => d.outcome)).toEqual(['silent', 'emitted']);
  });
});
