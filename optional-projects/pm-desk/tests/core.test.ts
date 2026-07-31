import { describe, expect, it } from 'vitest';

import {
  ConfigError,
  PmDeskError,
  SchemaValidationError,
  isPmDeskError,
} from '../src/core/errors.js';
import { contentHash, normalizeText, sha256Hex, stableStringify } from '../src/core/hash.js';
import { deterministicId, randomId } from '../src/core/ids.js';
import { epochMs, isoFromEpochMs, nowIso, parseIsoToEpochMs } from '../src/core/time.js';

describe('errors', () => {
  it('carries a stable machine-readable code and actionable hint', () => {
    const err = new ConfigError('PM_DESK_INGRESS_SECRET is not set', {
      hint: 'Generate one with `openssl rand -hex 32` and export it.',
    });
    expect(err).toBeInstanceOf(PmDeskError);
    expect(err.code).toBe('CONFIG_ERROR');
    expect(err.hint).toContain('openssl rand -hex 32');
    expect(isPmDeskError(err)).toBe(true);
    expect(isPmDeskError(new Error('nope'))).toBe(false);
  });

  it('renders a CLI-friendly single line', () => {
    const err = new SchemaValidationError('signal envelope failed validation', {
      hint: 'Check the `kind` field.',
      details: { field: 'kind' },
    });
    expect(err.toCliString()).toBe(
      'SCHEMA_VALIDATION_ERROR: signal envelope failed validation\n  hint: Check the `kind` field.',
    );
    expect(err.details).toEqual({ field: 'kind' });
  });
});

describe('time', () => {
  it('produces UTC ISO-8601 with millisecond precision and a Z suffix', () => {
    const iso = nowIso();
    expect(iso).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/);
  });

  it('round-trips epoch millis and ISO strings', () => {
    const ms = 1_785_453_617_937;
    const iso = isoFromEpochMs(ms);
    expect(iso).toBe('2026-07-30T23:20:17.937Z');
    expect(parseIsoToEpochMs(iso)).toBe(ms);
    expect(epochMs(iso)).toBe(ms);
  });

  it('rejects unparseable timestamps with a typed error', () => {
    expect(() => parseIsoToEpochMs('not-a-time')).toThrow(PmDeskError);
  });
});

describe('hash', () => {
  it('hashes bytes and strings identically to sha256', () => {
    // Well-known vector: sha256("abc")
    expect(sha256Hex('abc')).toBe(
      'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad',
    );
  });

  it('normalizes whitespace, line endings and zero-width characters deterministically', () => {
    const a = normalizeText('  Hello\r\n\r\n  World ​ \n');
    const b = normalizeText('Hello\n\nWorld\n');
    expect(a).toBe('Hello\n\nWorld');
    expect(a).toBe(b);
  });

  it('collapses runs of blank lines so cosmetic reflow does not change the fingerprint', () => {
    expect(normalizeText('a\n\n\n\n\nb')).toBe('a\n\nb');
  });

  it('content hash is stable across equivalent-but-differently-formatted input', () => {
    expect(contentHash('Q1 GDP: 3.1%\r\n')).toBe(contentHash('  Q1 GDP: 3.1%  '));
    expect(contentHash('Q1 GDP: 3.1%')).not.toBe(contentHash('Q1 GDP: 3.2%'));
  });

  it('stable stringify is key-order independent', () => {
    expect(stableStringify({ b: 1, a: { d: 2, c: [3, { f: 4, e: 5 }] } })).toBe(
      '{"a":{"c":[3,{"e":5,"f":4}],"d":2},"b":1}',
    );
  });
});

describe('ids', () => {
  it('deterministic ids depend only on prefix and parts', () => {
    const a = deterministicId('sig', ['rule.v1', 'source-a', '2026-07-30T00:00:00.000Z']);
    const b = deterministicId('sig', ['rule.v1', 'source-a', '2026-07-30T00:00:00.000Z']);
    const c = deterministicId('sig', ['rule.v1', 'source-b', '2026-07-30T00:00:00.000Z']);
    expect(a).toBe(b);
    expect(a).not.toBe(c);
    expect(a).toMatch(/^sig_[0-9a-f]{32}$/);
  });

  it('does not confuse different part groupings', () => {
    expect(deterministicId('sig', ['ab', 'c'])).not.toBe(deterministicId('sig', ['a', 'bc']));
  });

  it('random ids are prefixed and unique', () => {
    const ids = new Set(Array.from({ length: 200 }, () => randomId('led')));
    expect(ids.size).toBe(200);
    expect([...ids][0]).toMatch(/^led_[0-9a-f]{24}$/);
  });
});
