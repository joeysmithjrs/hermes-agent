import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { MarketDataError } from '../src/core/errors.js';
import { fixedClock } from '../src/core/time.js';
import { collectMarkets, snapshotTokens } from '../src/polymarket/collector.js';
import { normalizeMarket, toIsoMs } from '../src/polymarket/normalize.js';
import { PollingMarketFeed, describeRealtimeCapability } from '../src/polymarket/realtime.js';
import { fetchTokenSnapshot } from '../src/polymarket/snapshot.js';
import { openStore, type DeskStore } from '../src/store/index.js';
import { FakePublicClient, FIXTURE_END_DATE, rawMarket } from './helpers/fake-polymarket.js';

let dir: string;
let store: DeskStore;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'pm-desk-pm-'));
  store = openStore({ home: dir });
});

afterEach(() => {
  store.close();
  rmSync(dir, { recursive: true, force: true });
});

describe('normalization', () => {
  it('upgrades second-precision SDK timestamps to the desk millisecond format', () => {
    expect(toIsoMs('2026-07-31T12:00:00Z')).toBe('2026-07-31T12:00:00.000Z');
    expect(toIsoMs('2025-05-02T15:48:10.582Z')).toBe('2025-05-02T15:48:10.582Z');
    expect(toIsoMs(null)).toBeNull();
    expect(toIsoMs('')).toBeNull();
  });

  it('normalizes a market record into market + outcome token rows', () => {
    const { market, tokens } = normalizeMarket(rawMarket(), '2026-07-30T12:00:00.000Z');

    expect(market.market_id).toBe('540817');
    expect(market.condition_id).toBe('0xcond540817');
    expect(market.question).toBe('New Rihanna Album before GTA VI?');
    expect(market.active).toBe(true);
    expect(market.closed).toBe(false);
    expect(market.end_date).toBe(FIXTURE_END_DATE.replace('Z', '.000Z'));
    expect(market.event_title).toBe('What will happen before GTA VI?');

    expect(tokens).toHaveLength(2);
    expect(tokens.find((t) => t.outcome === 'YES')?.token_id).toBe('tokenYES');
    expect(tokens.find((t) => t.outcome === 'NO')?.token_id).toBe('tokenNO');
  });

  it('rejects a record with no usable market id rather than storing a blank row', () => {
    expect(() => normalizeMarket({ question: 'x' }, '2026-07-30T12:00:00.000Z')).toThrow(
      MarketDataError,
    );
  });

  it('tolerates a market with no outcome tokens (not yet tradable)', () => {
    const raw = rawMarket();
    delete (raw as Record<string, unknown>).outcomes;
    const { tokens } = normalizeMarket(raw, '2026-07-30T12:00:00.000Z');
    expect(tokens).toEqual([]);
  });
});

describe('snapshot', () => {
  it('derives BBO and spread from the book regardless of level ordering', async () => {
    const client = new FakePublicClient();
    const snapshot = await fetchTokenSnapshot(client, 'tokenYES', {
      clock: fixedClock('2026-07-30T12:00:00.000Z'),
    });

    expect(snapshot.observed_at).toBe('2026-07-30T12:00:00.000Z');
    expect(snapshot.mid).toBe(0.505);
    expect(snapshot.best_bid).toBe(0.5); // highest bid, not the last one listed
    expect(snapshot.best_ask).toBe(0.51); // lowest ask
    expect(snapshot.spread).toBeCloseTo(0.01, 10);
    expect(snapshot.book_available).toBe(true);
  });

  it('reports missing book data as nulls plus a typed capability note, not zeros', async () => {
    const client = new FakePublicClient({ bookUnavailable: true });
    const snapshot = await fetchTokenSnapshot(client, 'tokenYES', {
      clock: fixedClock('2026-07-30T12:00:00.000Z'),
    });

    expect(snapshot.book_available).toBe(false);
    expect(snapshot.best_bid).toBeNull();
    expect(snapshot.best_ask).toBeNull();
    expect(snapshot.spread).toBeNull();
    expect(snapshot.mid).toBe(0.505); // midpoint endpoint still answered
    expect(snapshot.unavailable.map((u) => u.capability)).toContain('order_book');
  });

  it('surfaces a total data outage as a typed error rather than a fabricated snapshot', async () => {
    const client = new FakePublicClient({ midpointUnavailable: true, bookUnavailable: true });
    await expect(
      fetchTokenSnapshot(client, 'tokenYES', { clock: fixedClock('2026-07-30T12:00:00.000Z') }),
    ).rejects.toThrow(MarketDataError);
  });

  it('prefers an empty book over an invented one when both sides are empty', async () => {
    const client = new FakePublicClient({ emptyBook: true });
    const snapshot = await fetchTokenSnapshot(client, 'tokenYES', {
      clock: fixedClock('2026-07-30T12:00:00.000Z'),
    });
    expect(snapshot.best_bid).toBeNull();
    expect(snapshot.best_ask).toBeNull();
    expect(snapshot.book_available).toBe(false);
  });
});

describe('collector', () => {
  it('discovers markets with a bounded limit and upserts them into the store', async () => {
    const client = new FakePublicClient({ marketCount: 25 });
    const result = await collectMarkets(store, client, {
      limit: 7,
      pageSize: 3,
      clock: fixedClock('2026-07-30T12:00:00.000Z'),
    });

    expect(result.markets).toBe(7);
    expect(result.tokens).toBe(14);
    expect(store.markets.countMarkets()).toBe(7);
    // Bounded paging: never fetches more pages than the limit requires.
    expect(client.pagesFetched).toBe(3);
  });

  it('dry run reports what it would write without touching the store', async () => {
    const client = new FakePublicClient({ marketCount: 5 });
    const result = await collectMarkets(store, client, {
      limit: 5,
      dryRun: true,
      clock: fixedClock('2026-07-30T12:00:00.000Z'),
    });

    expect(result.markets).toBe(5);
    expect(result.dryRun).toBe(true);
    expect(store.markets.countMarkets()).toBe(0);
  });

  it('re-running discovery is idempotent on market identity', async () => {
    const client = new FakePublicClient({ marketCount: 4 });
    const opts = { limit: 4, clock: fixedClock('2026-07-30T12:00:00.000Z') };
    await collectMarkets(store, client, opts);
    await collectMarkets(store, client, opts);
    expect(store.markets.countMarkets()).toBe(4);
  });

  it('appends one observation per requested token', async () => {
    const client = new FakePublicClient({ marketCount: 2 });
    await collectMarkets(store, client, {
      limit: 2,
      clock: fixedClock('2026-07-30T12:00:00.000Z'),
    });

    const result = await snapshotTokens(store, client, ['tokenYES-0', 'tokenYES-1'], {
      clock: fixedClock('2026-07-30T12:05:00.000Z'),
    });

    expect(result.observed).toBe(2);
    expect(result.failed).toHaveLength(0);
    expect(store.observations.latest('tokenYES-0')?.observed_at).toBe('2026-07-30T12:05:00.000Z');
  });

  it('records per-token failures without aborting the whole sweep', async () => {
    const client = new FakePublicClient({ marketCount: 2, failTokens: ['tokenYES-1'] });
    await collectMarkets(store, client, {
      limit: 2,
      clock: fixedClock('2026-07-30T12:00:00.000Z'),
    });

    const result = await snapshotTokens(store, client, ['tokenYES-0', 'tokenYES-1'], {
      clock: fixedClock('2026-07-30T12:05:00.000Z'),
    });

    expect(result.observed).toBe(1);
    expect(result.failed).toHaveLength(1);
    expect(result.failed[0]?.token_id).toBe('tokenYES-1');
  });
});

describe('realtime capability', () => {
  it('reports whether the injected client exposes public subscriptions', () => {
    expect(describeRealtimeCapability(new FakePublicClient()).subscriptions).toBe(false);
    expect(
      describeRealtimeCapability(new FakePublicClient({ supportsSubscribe: true })).subscriptions,
    ).toBe(true);
  });

  it('polling feed emits one observation per tick and stops cleanly', async () => {
    const client = new FakePublicClient({ marketCount: 1 });
    const seen: string[] = [];
    let now = Date.parse('2026-07-30T12:00:00.000Z');

    const feed = new PollingMarketFeed(client, {
      tokenIds: ['tokenYES-0'],
      intervalMs: 1000,
      clock: { now: () => new Date(now).toISOString() },
    });
    feed.on((observation) => seen.push(observation.observed_at));

    await feed.tick();
    now += 1000;
    await feed.tick();
    feed.stop();
    await feed.tick(); // no-op after stop

    expect(seen).toEqual(['2026-07-30T12:00:00.000Z', '2026-07-30T12:00:01.000Z']);
  });

  it('polling feed surfaces errors to a handler instead of throwing into the loop', async () => {
    const client = new FakePublicClient({ marketCount: 1, failTokens: ['tokenYES-0'] });
    const errors: string[] = [];
    const feed = new PollingMarketFeed(client, {
      tokenIds: ['tokenYES-0'],
      intervalMs: 1000,
      clock: fixedClock('2026-07-30T12:00:00.000Z'),
    });
    feed.onError((err) => errors.push(err.message));

    await feed.tick();
    expect(errors).toHaveLength(1);
  });
});
