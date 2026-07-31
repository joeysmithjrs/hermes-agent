import { describe, expect, it } from 'vitest';

import { SchemaValidationError } from '../src/core/errors.js';
import { parseAdjudication } from '../src/schema/adjudication.js';
import { parseMonitorSpec } from '../src/schema/monitor-spec.js';
import { parseSignalEnvelope, SIGNAL_ENVELOPE_VERSION } from '../src/schema/signal.js';
import { parseSourceSpec } from '../src/schema/source-spec.js';

function validSignal(overrides: Record<string, unknown> = {}) {
  return {
    version: SIGNAL_ENVELOPE_VERSION,
    signal_id: 'sig_0123456789abcdef0123456789abcdef',
    kind: 'primary_source_change',
    severity: 'high',
    observed_at: '2026-07-30T12:00:00.000Z',
    rule_id: 'gdp_release_change',
    rule_version: '1',
    market_refs: [
      {
        market_id: '540817',
        condition_id: '0x1fad72fae204143ff1c3035e99e7c0f65ea8d5cd9bd1070987bd1a3316f772be',
        token_id: '98022490269692409998126496127597032490334070080325855126491859374983463996227',
        outcome: 'YES',
      },
    ],
    source_refs: [
      {
        source_id: 'example_official_release',
        url: 'https://example.gov/release',
        previous_hash: 'a'.repeat(64),
        current_hash: 'b'.repeat(64),
        artifact_ref: 'sha256:' + 'b'.repeat(64),
      },
    ],
    market_snapshot: {
      observed_at: '2026-07-30T11:59:30.000Z',
      mid: 0.505,
      best_bid: 0.5,
      best_ask: 0.51,
      spread: 0.01,
    },
    evidence: {
      diff_excerpt: '- Q1 GDP: 3.1%\n+ Q1 GDP: 2.4%',
      claims: ['Headline Q1 GDP revised down from 3.1% to 2.4%'],
    },
    paper_only: true,
    dedupe_key: 'gdp_release_change:v1:' + 'b'.repeat(64),
    ...overrides,
  };
}

describe('SignalEnvelope schema', () => {
  it('accepts a fully-populated envelope and returns a typed value', () => {
    const signal = parseSignalEnvelope(validSignal());
    expect(signal.kind).toBe('primary_source_change');
    expect(signal.paper_only).toBe(true);
    expect(signal.market_snapshot?.mid).toBe(0.505);
  });

  it('rejects paper_only:false — the invariant is structural, not advisory', () => {
    expect(() => parseSignalEnvelope(validSignal({ paper_only: false }))).toThrow(
      SchemaValidationError,
    );
  });

  it('rejects an unknown kind and names the offending path', () => {
    try {
      parseSignalEnvelope(validSignal({ kind: 'execute_trade' }));
      expect.unreachable('should have thrown');
    } catch (err) {
      expect(err).toBeInstanceOf(SchemaValidationError);
      expect((err as SchemaValidationError).message).toContain('kind');
    }
  });

  it('rejects unknown top-level fields so envelope drift is caught at the boundary', () => {
    expect(() => parseSignalEnvelope(validSignal({ order_size: 100 }))).toThrow(
      SchemaValidationError,
    );
  });

  it('rejects a non-UTC / second-precision timestamp', () => {
    expect(() =>
      parseSignalEnvelope(validSignal({ observed_at: '2026-07-30T12:00:00+02:00' })),
    ).toThrow(SchemaValidationError);
  });

  it('rejects probabilities outside [0,1]', () => {
    const bad = validSignal();
    (bad.market_snapshot as Record<string, unknown>).mid = 1.7;
    expect(() => parseSignalEnvelope(bad)).toThrow(SchemaValidationError);
  });

  it('requires at least one reference so every signal is auditable', () => {
    expect(() => parseSignalEnvelope(validSignal({ source_refs: [], market_refs: [] }))).toThrow(
      SchemaValidationError,
    );
  });

  it('allows a market_move signal with no source refs', () => {
    const signal = parseSignalEnvelope(
      validSignal({
        kind: 'market_move',
        source_refs: [],
        evidence: { claims: ['mid moved 0.505 -> 0.610 in 42m'] },
      }),
    );
    expect(signal.source_refs).toEqual([]);
  });

  it('rejects a future version it cannot reason about', () => {
    expect(() => parseSignalEnvelope(validSignal({ version: 2 }))).toThrow(SchemaValidationError);
  });
});

describe('SourceSpec schema', () => {
  const spec = {
    id: 'example_official_release',
    url: 'https://example.gov/release',
    allowed_domains: ['example.gov'],
    wait_for: 'main',
    extract: {
      text_selector: 'main',
      fields: { title: 'h1', published_at: 'time' },
    },
    fingerprint: 'normalized_text_sha256',
  };

  it('accepts the documented spec shape', () => {
    const parsed = parseSourceSpec(spec);
    expect(parsed.id).toBe('example_official_release');
    expect(parsed.version).toBe(1);
    expect(parsed.extract.fields.title).toBe('h1');
  });

  it('rejects non-HTTPS urls', () => {
    expect(() => parseSourceSpec({ ...spec, url: 'http://example.gov/release' })).toThrow(
      SchemaValidationError,
    );
  });

  it('requires a non-empty allowed_domains list', () => {
    expect(() => parseSourceSpec({ ...spec, allowed_domains: [] })).toThrow(SchemaValidationError);
  });

  it('rejects an unsupported fingerprint algorithm', () => {
    expect(() => parseSourceSpec({ ...spec, fingerprint: 'md5' })).toThrow(SchemaValidationError);
  });
});

describe('MonitorSpec schema', () => {
  it('parses a primary_source_change spec', () => {
    const parsed = parseMonitorSpec({
      id: 'gdp_release_change',
      version: 1,
      kind: 'primary_source_change',
      severity: 'high',
      cooldown_s: 3600,
      params: { source_id: 'example_official_release' },
    });
    expect(parsed.kind).toBe('primary_source_change');
    expect(parsed.enabled).toBe(true);
  });

  it('parses a market_move spec with thresholds', () => {
    const parsed = parseMonitorSpec({
      id: 'mid_move',
      version: 1,
      kind: 'market_move',
      severity: 'warn',
      cooldown_s: 900,
      params: {
        token_id: '1234',
        abs_move: 0.05,
        lookback_s: 3600,
        max_staleness_s: 900,
      },
    });
    expect(parsed.params).toMatchObject({ abs_move: 0.05 });
  });

  it('rejects a market_move spec with no threshold configured', () => {
    expect(() =>
      parseMonitorSpec({
        id: 'mid_move',
        version: 1,
        kind: 'market_move',
        severity: 'warn',
        cooldown_s: 900,
        params: { token_id: '1234', lookback_s: 3600 },
      }),
    ).toThrow(SchemaValidationError);
  });

  it('rejects params that do not match the declared kind', () => {
    expect(() =>
      parseMonitorSpec({
        id: 'mismatch',
        version: 1,
        kind: 'primary_source_change',
        severity: 'info',
        cooldown_s: 60,
        params: { token_id: '1234', abs_move: 0.1 },
      }),
    ).toThrow(SchemaValidationError);
  });
});

describe('Adjudication schema', () => {
  const base = {
    version: 1,
    signal_id: 'sig_0123456789abcdef0123456789abcdef',
    decision: 'watch',
    rationale: 'Source moved but the linked market resolves on a different statistic.',
    alignment: {
      market_source_aligned: false,
      notes: 'Release covers Q1; market asks about Q2.',
    },
    novelty: 'novel',
    still_live: true,
    invalidation: 'A Q2 release supersedes this.',
    telegram_message: 'PAPER ONLY — watch',
    paper_only: true,
  };

  it('accepts the three allowed decisions and nothing else', () => {
    for (const decision of ['ignore', 'watch', 'paper_alert']) {
      expect(parseAdjudication({ ...base, decision }).decision).toBe(decision);
    }
    expect(() => parseAdjudication({ ...base, decision: 'place_order' })).toThrow(
      SchemaValidationError,
    );
    expect(() => parseAdjudication({ ...base, decision: 'buy' })).toThrow(SchemaValidationError);
  });

  it('requires paper_only to be true', () => {
    expect(() => parseAdjudication({ ...base, paper_only: false })).toThrow(SchemaValidationError);
  });

  it('requires a paper_alert to carry a ledger proposal', () => {
    expect(() => parseAdjudication({ ...base, decision: 'paper_alert' })).not.toThrow();
    const parsed = parseAdjudication({
      ...base,
      decision: 'paper_alert',
      ledger_proposal: {
        thesis: 'Downward GDP revision is not yet in the mid.',
        candidate_outcome: 'YES',
        assumed_size_usd: 100,
        slippage_rule: 'cross_spread_full',
        expiry_horizon_s: 86400,
        markout_horizons_s: [300, 3600],
        invalidations: ['Market resolves before markout'],
      },
    });
    expect(parsed.ledger_proposal?.assumed_size_usd).toBe(100);
  });
});
