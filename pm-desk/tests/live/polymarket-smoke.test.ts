import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterAll, beforeAll, describe, expect, it } from 'vitest';

import { createDeskPublicClient } from '../../src/polymarket/client.js';
import { collectMarkets, snapshotTokens } from '../../src/polymarket/collector.js';
import { describeRealtimeCapability } from '../../src/polymarket/realtime.js';
import { openStore, type DeskStore } from '../../src/store/index.js';

/**
 * OPT-IN live smoke against the official Polymarket *public* endpoints.
 *
 * Excluded from the default test run (see vitest.config.ts) and additionally
 * gated on PM_DESK_LIVE=1, so `npm test` never touches the network. Needs no
 * credentials — it only exercises the public client with a tiny limit.
 *
 *   PM_DESK_LIVE=1 npm run test:live
 */
const LIVE = process.env.PM_DESK_LIVE === '1';
const suite = LIVE ? describe : describe.skip;

let dir: string;
let store: DeskStore;

beforeAll(() => {
  if (!LIVE) return;
  dir = mkdtempSync(join(tmpdir(), 'pm-desk-live-'));
  store = openStore({ home: dir });
});

afterAll(() => {
  if (!LIVE) return;
  store.close();
  rmSync(dir, { recursive: true, force: true });
});

suite('polymarket public live smoke', () => {
  it('discovers a couple of real open markets through the official public client', async () => {
    const client = createDeskPublicClient();
    const result = await collectMarkets(store, client, { limit: 2, pageSize: 2 });

    expect(result.markets).toBe(2);
    expect(store.markets.countMarkets()).toBe(2);

    const stored = store.markets.listMarkets({ limit: 2 });
    expect(stored[0]?.question.length).toBeGreaterThan(0);
    expect(stored[0]?.condition_id).toMatch(/^0x[0-9a-f]+$/i);
  }, 60_000);

  it('takes a real snapshot for one discovered outcome token', async () => {
    const client = createDeskPublicClient();
    const market = store.markets.listMarkets({ limit: 1 })[0];
    expect(market).toBeDefined();
    const token = store.markets.listTokens(market!.market_id)[0];
    expect(token).toBeDefined();

    const result = await snapshotTokens(store, client, [token!.token_id]);
    expect(result.observed).toBe(1);

    const observation = store.observations.latest(token!.token_id);
    expect(observation).toBeDefined();
    // A real market always has at least one of these; nulls are legal, both aren't.
    expect(observation!.mid !== null || observation!.book_available).toBe(true);
  }, 60_000);

  it('reports the realtime capability of the real client', () => {
    const capability = describeRealtimeCapability(createDeskPublicClient());
    expect(capability.active).toBe('polling');
    expect(typeof capability.subscriptions).toBe('boolean');
  });
});
