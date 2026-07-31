import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { LedgerInvariantError } from '../src/core/errors.js';
import { fixedClock } from '../src/core/time.js';
import { recordFromAdjudication, recordManualEntry } from '../src/ledger/ledger.js';
import { renderTelegramAlert } from '../src/ledger/render.js';
import { parseAdjudication } from '../src/schema/adjudication.js';
import { openStore, type DeskStore } from '../src/store/index.js';

let dir: string;
let store: DeskStore;

const NOW = '2026-07-30T12:00:00.000Z';
const SIGNAL_ID = 'sig_' + '1'.repeat(32);

const SIGNAL = {
  version: 1 as const,
  signal_id: SIGNAL_ID,
  kind: 'source_market_divergence' as const,
  severity: 'high' as const,
  observed_at: '2026-07-30T11:55:00.000Z',
  rule_id: 'gdp_vs_market',
  rule_version: '1',
  market_refs: [
    {
      market_id: 'm1',
      condition_id: '0xcond',
      token_id: 'tok1',
      outcome: 'YES' as const,
      question: 'Will Q1 GDP be revised below 3.0%?',
      end_date: '2026-08-05T12:00:00.000Z',
    },
  ],
  source_refs: [
    {
      source_id: 'example_official_release',
      url: 'https://example.gov/release',
      previous_hash: 'a'.repeat(64),
      current_hash: 'b'.repeat(64),
      artifact_ref: 'sha256:' + 'b'.repeat(64),
      collected_at: '2026-07-30T11:55:00.000Z',
    },
  ],
  market_snapshot: {
    observed_at: '2026-07-30T11:58:00.000Z',
    mid: 0.32,
    best_bid: 0.31,
    best_ask: 0.33,
    spread: 0.02,
    book_available: true,
  },
  evidence: {
    diff_excerpt: '- 3.1 percent\n+ 2.4 percent',
    claims: ['Q1 GDP revised down from 3.1% to 2.4%.'],
  },
  paper_only: true as const,
  dedupe_key: 'gdp_vs_market:1:' + 'b'.repeat(64),
};

const adjudication = (overrides: Record<string, unknown> = {}) =>
  parseAdjudication({
    version: 1,
    signal_id: SIGNAL_ID,
    decision: 'paper_alert',
    rationale:
      'The release revises Q1 GDP to 2.4%, which is the exact statistic the market resolves on.',
    alignment: {
      market_source_aligned: true,
      notes: 'Market resolves on the BEA Q1 figure; this is that figure.',
      resolution_mapping: 'Resolves YES if the revised figure is below 3.0%.',
    },
    novelty: 'novel',
    still_live: true,
    invalidation: 'A subsequent BEA revision above 3.0% before 2026-08-05.',
    telegram_message: 'PAPER ONLY — GDP revised to 2.4%',
    ledger_proposal: {
      thesis: 'The 2.4% print is below 3.0% and the market has not repriced.',
      candidate_outcome: 'YES',
      assumed_size_usd: 100,
      slippage_rule: 'cross_spread_full',
      expiry_horizon_s: 86_400,
      markout_horizons_s: [300, 3600],
      invalidations: ['A later revision above 3.0%', 'Market resolves before markout'],
    },
    paper_only: true,
    ...overrides,
  });

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'pm-desk-led-'));
  store = openStore({ home: dir });
  store.signals.record(SIGNAL, 'monitor');
});

afterEach(() => {
  store.close();
  rmSync(dir, { recursive: true, force: true });
});

describe('ledger creation from adjudication', () => {
  it('records a paper entry with full provenance and derived entry price', () => {
    const entry = recordFromAdjudication(store, adjudication(), { clock: fixedClock(NOW) });

    expect(entry.entry_id).toMatch(/^led_[0-9a-f]{24}$/);
    expect(entry.signal_id).toBe(SIGNAL_ID);
    expect(entry.origin).toBe('adjudication');
    expect(entry.market_id).toBe('m1');
    expect(entry.condition_id).toBe('0xcond');
    expect(entry.token_id).toBe('tok1');
    expect(entry.outcome).toBe('YES');
    expect(entry.decided_at).toBe(NOW);
    expect(entry.expires_at).toBe('2026-07-31T12:00:00.000Z');
    expect(entry.thesis_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(entry.entry_observed_at).toBe('2026-07-30T11:58:00.000Z');
    // cross_spread_full on a YES buy pays the ask.
    expect(entry.assumed_entry_price).toBe(0.33);
    expect(entry.evidence_refs).toContain('sha256:' + 'b'.repeat(64));
  });

  it('applies each slippage rule to the recorded entry observation', () => {
    const mid = recordFromAdjudication(
      store,
      adjudication({
        ledger_proposal: { ...adjudication().ledger_proposal, slippage_rule: 'mid_no_slippage' },
      }),
      { clock: fixedClock(NOW) },
    );
    expect(mid.assumed_entry_price).toBe(0.32);
  });

  it('is impossible to label as a real fill', () => {
    const entry = recordFromAdjudication(store, adjudication(), { clock: fixedClock(NOW) });
    expect(entry.paper_only).toBe(true);
    expect(entry.fill_type).toBe('SIMULATED_NO_FILL');

    // The database itself refuses any other value.
    expect(() =>
      store.db
        .prepare('UPDATE paper_ledger SET fill_type = ? WHERE entry_id = ?')
        .run('REAL_FILL', entry.entry_id),
    ).toThrow();
    expect(() =>
      store.db
        .prepare('UPDATE paper_ledger SET paper_only = 0 WHERE entry_id = ?')
        .run(entry.entry_id),
    ).toThrow();
  });

  it('refuses any decision other than paper_alert', () => {
    for (const decision of ['ignore', 'watch']) {
      expect(() =>
        recordFromAdjudication(store, adjudication({ decision, ledger_proposal: undefined }), {
          clock: fixedClock(NOW),
        }),
      ).toThrow(LedgerInvariantError);
    }
    expect(store.db.prepare('SELECT COUNT(*) AS n FROM paper_ledger').get()).toEqual({ n: 0 });
  });

  it('refuses a paper_alert with no ledger proposal', () => {
    expect(() =>
      recordFromAdjudication(store, adjudication({ ledger_proposal: undefined }), {
        clock: fixedClock(NOW),
      }),
    ).toThrow(LedgerInvariantError);
  });

  it('refuses an adjudication whose signal is not in the store', () => {
    expect(() =>
      recordFromAdjudication(store, adjudication({ signal_id: 'sig_' + '9'.repeat(32) }), {
        clock: fixedClock(NOW),
      }),
    ).toThrow(LedgerInvariantError);
  });

  it('refuses when the slippage rule needs a price the snapshot does not have', () => {
    const noBookSignal = {
      ...SIGNAL,
      signal_id: 'sig_' + '5'.repeat(32),
      dedupe_key: 'other',
      market_snapshot: {
        observed_at: '2026-07-30T11:58:00.000Z',
        mid: null,
        best_bid: null,
        best_ask: null,
        spread: null,
        book_available: false,
      },
    };
    store.signals.record(noBookSignal, 'monitor');

    expect(() =>
      recordFromAdjudication(store, adjudication({ signal_id: noBookSignal.signal_id }), {
        clock: fixedClock(NOW),
      }),
    ).toThrow(LedgerInvariantError);
  });

  it('records the same adjudication only once', () => {
    recordFromAdjudication(store, adjudication(), { clock: fixedClock(NOW) });
    expect(() => recordFromAdjudication(store, adjudication(), { clock: fixedClock(NOW) })).toThrow(
      LedgerInvariantError,
    );
  });
});

describe('manual operator entries', () => {
  it('requires an explicit operator acknowledgement', () => {
    expect(() =>
      recordManualEntry(
        store,
        {
          thesis: 'Manual paper observation for the Q1 GDP market.',
          market_id: 'm1',
          token_id: 'tok1',
          outcome: 'YES',
          assumed_size_usd: 50,
          slippage_rule: 'mid_no_slippage',
          expiry_horizon_s: 3600,
          markout_horizons_s: [300],
          invalidations: ['manual review'],
          entry_mid: 0.4,
          acknowledged: false,
        },
        { clock: fixedClock(NOW) },
      ),
    ).toThrow(LedgerInvariantError);
  });

  it('records an acknowledged manual entry, still flagged paper-only', () => {
    const entry = recordManualEntry(
      store,
      {
        thesis: 'Manual paper observation for the Q1 GDP market.',
        market_id: 'm1',
        token_id: 'tok1',
        outcome: 'YES',
        assumed_size_usd: 50,
        slippage_rule: 'mid_no_slippage',
        expiry_horizon_s: 3600,
        markout_horizons_s: [300],
        invalidations: ['manual review'],
        entry_mid: 0.4,
        acknowledged: true,
      },
      { clock: fixedClock(NOW) },
    );

    expect(entry.origin).toBe('manual_operator');
    expect(entry.signal_id).toBeNull();
    expect(entry.fill_type).toBe('SIMULATED_NO_FILL');
    expect(entry.assumed_entry_price).toBe(0.4);
  });
});

describe('annotations', () => {
  it('appends markouts and outcomes without mutating the original entry', () => {
    const entry = recordFromAdjudication(store, adjudication(), { clock: fixedClock(NOW) });

    store.ledger.annotate({
      entry_id: entry.entry_id,
      recorded_at: '2026-07-30T12:05:00.000Z',
      kind: 'markout',
      note: '5-minute markout',
      detail: { horizon_s: 300, mid: 0.41, pnl_paper_usd: 24.24 },
    });
    store.ledger.annotate({
      entry_id: entry.entry_id,
      recorded_at: '2026-08-05T12:00:00.000Z',
      kind: 'outcome',
      note: 'Resolved YES',
    });

    const annotations = store.ledger.listAnnotations(entry.entry_id);
    expect(annotations.map((a) => a.kind)).toEqual(['markout', 'outcome']);
    expect(store.ledger.get(entry.entry_id)?.assumed_entry_price).toBe(0.33);
  });

  it('refuses an annotation for an entry that does not exist', () => {
    expect(() =>
      store.ledger.annotate({
        entry_id: 'led_deadbeefdeadbeefdeadbeef',
        recorded_at: NOW,
        kind: 'note',
        note: 'orphan',
      }),
    ).toThrow();
  });
});

describe('Telegram rendering', () => {
  it('renders every required element and shouts PAPER ONLY', () => {
    const entry = recordFromAdjudication(store, adjudication(), { clock: fixedClock(NOW) });
    const text = renderTelegramAlert({ entry, signal: SIGNAL, adjudication: adjudication() });

    expect(text).toContain('PAPER ONLY');
    expect(text.split('\n')[0]).toContain('PAPER ONLY');

    expect(text).toContain('HIGH'); // severity
    expect(text).toContain('Will Q1 GDP be revised below 3.0%?'); // market question
    expect(text).toContain('YES'); // outcome
    expect(text).toContain('https://example.gov/release'); // source url
    expect(text).toContain('2026-07-30T11:55:00.000Z'); // source timestamp
    expect(text).toContain('2026-07-30T11:58:00.000Z'); // market observation time
    expect(text).toContain('0.32'); // mid
    expect(text).toContain('0.02'); // spread
    expect(text).toMatch(/revised below 3\.0%|2\.4%/); // adjudication conclusion
    expect(text).toContain('A subsequent BEA revision above 3.0%'); // invalidation
    expect(text).toContain('sha256:'); // artifact reference
    expect(text).toContain(entry.entry_id);
  });

  it('never suggests an executable action', () => {
    const entry = recordFromAdjudication(store, adjudication(), { clock: fixedClock(NOW) });
    const text = renderTelegramAlert({ entry, signal: SIGNAL, adjudication: adjudication() });
    const lower = text.toLowerCase();

    for (const phrase of [
      'buy now',
      'place order',
      'execute',
      'submit order',
      'wallet',
      'connect',
    ]) {
      expect(lower).not.toContain(phrase);
    }

    // "fill" may appear only inside the SIMULATED_NO_FILL marker, which asserts
    // the opposite of an execution instruction.
    const fillOccurrences = lower.match(/fill/g) ?? [];
    const markerOccurrences = lower.match(/simulated_no_fill|fill_type/g) ?? [];
    expect(fillOccurrences.length).toBe(markerOccurrences.length);

    expect(text).toContain('No order exists. Nothing was sent to any venue.');
  });

  it('fits inside a single Telegram message', () => {
    const entry = recordFromAdjudication(store, adjudication(), { clock: fixedClock(NOW) });
    const text = renderTelegramAlert({ entry, signal: SIGNAL, adjudication: adjudication() });
    expect(text.length).toBeLessThanOrEqual(4096);
  });

  it('renders a watch decision with no ledger entry', () => {
    const text = renderTelegramAlert({
      signal: SIGNAL,
      adjudication: adjudication({ decision: 'watch', ledger_proposal: undefined }),
    });
    expect(text).toContain('PAPER ONLY');
    expect(text).toContain('WATCH');
    expect(text).not.toContain('led_');
  });

  it('degrades gracefully when the market snapshot is missing', () => {
    const text = renderTelegramAlert({
      signal: { ...SIGNAL, market_snapshot: null },
      adjudication: adjudication({ decision: 'watch', ledger_proposal: undefined }),
    });
    expect(text).toContain('no market observation');
  });
});
