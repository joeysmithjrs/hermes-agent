import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { runCalibration } from '../src/research/cpi_nowcast/index.js';
import { bucketProbabilities, calibrateBucket } from '../src/research/cpi_nowcast/calibrate.js';
import { compareEdge } from '../src/research/cpi_nowcast/compare.js';
import { computeDiagnostics, joinResiduals } from '../src/research/cpi_nowcast/residuals.js';
import { bucketLabel, roundToOneDecimal } from '../src/research/cpi_nowcast/round.js';
import type {
  NowcastRow,
  PrintRow,
  ResidualDiagnostics,
} from '../src/research/cpi_nowcast/types.js';
import {
  loadNowcastsCsv,
  loadPrintsCsv,
  parseCsv,
} from '../src/research/cpi_nowcast/loaders/index.js';

const D = (s: string) => new Date(`${s}T00:00:00.000Z`);
const FIX = join(import.meta.dirname, '..', 'fixtures', 'cpi_nowcast');

function diag(residuals: number[]): ResidualDiagnostics {
  return computeDiagnostics(
    residuals.map((r, i) => ({
      refMonth: `2024-${String(i + 1).padStart(2, '0')}`,
      nowcast: 0,
      print: r,
      residual: r,
      vintageDate: D('2024-01-01'),
      releaseDate: D('2024-02-01'),
    })),
  );
}

describe('rounding', () => {
  it('rounds half-up at the second decimal (BLS-style display)', () => {
    expect(roundToOneDecimal(3.35)).toBe(3.4);
    expect(roundToOneDecimal(3.34)).toBe(3.3);
    expect(roundToOneDecimal(3.25)).toBe(3.3);
    expect(roundToOneDecimal(3.44)).toBe(3.4);
    expect(roundToOneDecimal(3.45)).toBe(3.5);
    expect(roundToOneDecimal(3.049)).toBe(3.0);
    expect(roundToOneDecimal(3.0)).toBe(3.0);
  });

  it('label always shows one decimal', () => {
    expect(bucketLabel(3.4)).toBe('3.4');
    expect(bucketLabel(3.0)).toBe('3.0');
    expect(bucketLabel(roundToOneDecimal(3.35))).toBe('3.4');
  });
});

describe('residual join', () => {
  it('pairs by refMonth and computes residual = print - nowcast', () => {
    const nowcasts: NowcastRow[] = [
      { refMonth: '2024-01', vintageDate: D('2024-01-15'), nowcast: 3.4 },
    ];
    const prints: PrintRow[] = [{ refMonth: '2024-01', releaseDate: D('2024-02-12'), yoy: 3.45 }];
    const { points, droppedLookahead } = joinResiduals(nowcasts, prints);
    expect(points).toHaveLength(1);
    expect(points[0]!.residual).toBeCloseTo(0.05, 10);
    expect(droppedLookahead).toBe(0);
  });

  it('drops look-ahead nowcasts (vintage not before release)', () => {
    const nowcasts: NowcastRow[] = [
      { refMonth: '2024-01', vintageDate: D('2024-02-20'), nowcast: 3.4 }, // after release → dropped
      { refMonth: '2024-01', vintageDate: D('2024-01-15'), nowcast: 3.38 }, // eligible
    ];
    const prints: PrintRow[] = [{ refMonth: '2024-01', releaseDate: D('2024-02-12'), yoy: 3.45 }];
    const { points, droppedLookahead } = joinResiduals(nowcasts, prints);
    expect(points).toHaveLength(1);
    expect(points[0]!.nowcast).toBe(3.38); // latest eligible wins
    expect(droppedLookahead).toBe(1);
  });

  it('picks the latest eligible vintage when multiple precede the release', () => {
    const nowcasts: NowcastRow[] = [
      { refMonth: '2024-01', vintageDate: D('2024-01-10'), nowcast: 3.3 },
      { refMonth: '2024-01', vintageDate: D('2024-01-20'), nowcast: 3.41 }, // latest eligible
      { refMonth: '2024-01', vintageDate: D('2024-02-20'), nowcast: 3.5 }, // look-ahead, dropped
    ];
    const prints: PrintRow[] = [{ refMonth: '2024-01', releaseDate: D('2024-02-12'), yoy: 3.45 }];
    const { points, droppedLookahead } = joinResiduals(nowcasts, prints);
    expect(points[0]!.nowcast).toBe(3.41);
    expect(droppedLookahead).toBe(1);
  });

  it('skips reference months with no print yet', () => {
    const nowcasts: NowcastRow[] = [
      { refMonth: '2024-02', vintageDate: D('2024-02-15'), nowcast: 3.4 },
    ];
    const prints: PrintRow[] = [{ refMonth: '2024-01', releaseDate: D('2024-02-12'), yoy: 3.45 }];
    const { points } = joinResiduals(nowcasts, prints);
    expect(points).toHaveLength(0);
  });

  it('diagnostics report RMSE and mean', () => {
    const d = diag([0.01, -0.01, 0.02, -0.02]);
    expect(d.sampleSize).toBe(4);
    expect(d.residualMean).toBeCloseTo(0, 10);
    expect(d.residualRmse).toBeCloseTo(Math.sqrt((0.0001 + 0.0001 + 0.0004 + 0.0004) / 4), 6);
  });
});

describe('calibration', () => {
  it('empirical bootstrap: mass lands in the bucket implied by each residual', () => {
    // live 3.42; residuals all in [-0.02, 0.02] → implied prints round to 3.4
    const d = diag([0.0, 0.02, -0.02, 0.01, -0.01]);
    const dist = bucketProbabilities(d, 3.42);
    const sum = [...dist.values()].reduce((a, b) => a + b.probability, 0);
    expect(sum).toBeCloseTo(1, 10);
    expect(dist.get('3.4')?.probability).toBe(1);
  });

  it('nearby buckets in the reported window sum to ~1 when support is inside it', () => {
    // live 3.42; residuals split between 3.4 and 3.5 buckets
    const d = diag([0.0, 0.0, 0.06, 0.06, -0.02, -0.02]);
    const { bucketProbs } = calibrateBucket(d, 3.4, { liveNowcast: 3.42 });
    const windowSum = Object.values(bucketProbs).reduce((a, b) => a + b, 0);
    // full support is {3.4, 3.5}; window is 3.3..3.5 so it captures everything.
    expect(windowSum).toBeCloseTo(1, 10);
    expect(bucketProbs['3.4']).toBeGreaterThan(0);
    expect(bucketProbs['3.5']).toBeGreaterThan(0);
  });

  it('returns 0 probability when there is no sample', () => {
    const d = diag([]);
    const { pBucket, bucketProbs } = calibrateBucket(d, 3.4, { liveNowcast: 3.42 });
    expect(pBucket).toBe(0);
    expect(Object.values(bucketProbs)).toEqual([0, 0, 0]);
  });
});

describe('edge decision', () => {
  const buffer = { halfSpread: 0.01, modelHaircut: 0.05 };
  const base = {
    pBucket: 0,
    mid: 0.5 as number | null,
    buffer,
    failClosed: false,
    failReason: null,
  };

  it('investigate_long when p clears mid by half-spread + haircut', () => {
    const r = compareEdge({ ...base, pBucket: 0.7, mid: 0.5 });
    expect(r.decision).toBe('investigate_long');
    expect(r.edgeVsMid).toBeCloseTo(0.2, 10);
  });

  it('investigate_short when mid clears p by the threshold', () => {
    const r = compareEdge({ ...base, pBucket: 0.2, mid: 0.5 });
    expect(r.decision).toBe('investigate_short');
  });

  it('no_trade inside the buffer band', () => {
    const r = compareEdge({ ...base, pBucket: 0.52, mid: 0.5 });
    expect(r.decision).toBe('no_trade');
  });

  it('fail_closed overrides any edge', () => {
    const r = compareEdge({
      ...base,
      pBucket: 0.99,
      mid: 0.01,
      failClosed: true,
      failReason: 'sample_size 3 < min_n 12',
    });
    expect(r.decision).toBe('fail_closed');
    expect(r.failReason).toBe('sample_size 3 < min_n 12');
  });

  it('no mid → no edge, no_trade (model-only report)', () => {
    const r = compareEdge({ ...base, pBucket: 0.9, mid: null });
    expect(r.decision).toBe('no_trade');
    expect(r.edgeVsMid).toBeNull();
  });
});

describe('runCalibration integration', () => {
  it('fails closed when sample_size < min_n', () => {
    const nowcasts: NowcastRow[] = [
      { refMonth: '2024-01', vintageDate: D('2024-01-15'), nowcast: 3.4 },
    ];
    const prints: PrintRow[] = [{ refMonth: '2024-01', releaseDate: D('2024-02-12'), yoy: 3.45 }];
    const r = runCalibration(nowcasts, prints, {
      liveNowcast: 3.42,
      bucket: 3.4,
      mid: 0.43,
    });
    expect(r.paper_only).toBe(true);
    expect(r.sample_size).toBe(1);
    expect(r.decision).toBe('fail_closed');
    expect(r.fail_reason).toContain('min_n');
  });

  it('produces an investigate_long edge on the shipped fixtures', () => {
    const nowcasts = loadNowcastsCsv(join(FIX, 'nowcasts.csv'));
    const prints = loadPrintsCsv(join(FIX, 'bls_yoy.csv'));
    const r = runCalibration(nowcasts, prints, {
      liveNowcast: 3.42,
      bucket: 3.4,
      mid: 0.43,
    });
    expect(r.sample_size).toBeGreaterThanOrEqual(12);
    expect(r.p_bucket).toBeGreaterThan(0.5);
    expect(r.decision).toBe('investigate_long');
    // all reported buckets are within [0,1]
    for (const p of Object.values(r.bucket_probs)) {
      expect(p).toBeGreaterThanOrEqual(0);
      expect(p).toBeLessThanOrEqual(1);
    }
  });
});

describe('loaders', () => {
  it('parses a simple CSV', () => {
    expect(parseCsv('a,b\n1,2\n3,4')).toEqual([
      ['a', 'b'],
      ['1', '2'],
      ['3', '4'],
    ]);
  });

  it('handles quoted fields with embedded commas', () => {
    expect(parseCsv('a,b\n"1,1",2')).toEqual([
      ['a', 'b'],
      ['1,1', '2'],
    ]);
  });

  it('loads the fixture nowcasts CSV', () => {
    const rows = loadNowcastsCsv(join(FIX, 'nowcasts.csv'));
    expect(rows.length).toBeGreaterThanOrEqual(12);
    expect(rows[0]!.refMonth).toMatch(/^\d{4}-\d{2}$/);
    expect(typeof rows[0]!.nowcast).toBe('number');
  });

  it('loads the fixture prints CSV', () => {
    const rows = loadPrintsCsv(join(FIX, 'bls_yoy.csv'));
    expect(rows.length).toBeGreaterThanOrEqual(12);
    expect(rows[0]!.refMonth).toMatch(/^\d{4}-\d{2}$/);
  });
});

describe('provenance and entry eligibility', () => {
  const nowcasts: NowcastRow[] = Array.from({ length: 24 }, (_, i) => ({
    refMonth: `${2023 + Math.floor(i / 12)}-${String((i % 12) + 1).padStart(2, '0')}`,
    vintageDate: D('2024-01-15'),
    nowcast: 3.4,
  }));
  const prints: PrintRow[] = nowcasts.map((n, i) => ({
    refMonth: n.refMonth,
    releaseDate: D('2024-06-12'),
    yoy: 3.4 + (i % 3) * 0.1,
  }));
  const base = { liveNowcast: 3.42, bucket: 3.4, mid: 0.2, minN: 12 };

  it('defaults to fixture provenance and blocks entry when the caller says nothing', () => {
    const r = runCalibration(nowcasts, prints, base);
    expect(r.series_provenance).toBe('fixture');
    expect(r.entry_eligible).toBe(false);
    expect(r.entry_block_reason).toContain('fixture');
  });

  it('leaves the decision label intact but still refuses entry on fixtures', () => {
    const r = runCalibration(nowcasts, prints, { ...base, seriesProvenance: 'fixture' });
    // The numbers are allowed to look like an edge; they are just not citable.
    expect(r.decision).toBe('investigate_long');
    expect(r.entry_eligible).toBe(false);
    expect(r.notes.some((n) => n.startsWith('research only:'))).toBe(true);
  });

  it('blocks mixed provenance unless a human overrides it', () => {
    const mixed = runCalibration(nowcasts, prints, { ...base, seriesProvenance: 'mixed' });
    expect(mixed.entry_eligible).toBe(false);
    expect(mixed.entry_block_reason).toContain('mixed');

    const overridden = runCalibration(nowcasts, prints, {
      ...base,
      seriesProvenance: 'mixed',
      allowMixedEntry: true,
    });
    expect(overridden.entry_eligible).toBe(true);
    expect(overridden.entry_block_reason).toBeNull();
  });

  it('allows entry on a live run with a real sample', () => {
    const r = runCalibration(nowcasts, prints, {
      ...base,
      seriesProvenance: 'live',
      sourceUrls: ['https://www.clevelandfed.org/example.json'],
    });
    expect(r.entry_eligible).toBe(true);
    expect(r.source_urls).toEqual(['https://www.clevelandfed.org/example.json']);
    expect(r.paired_n).toBe(r.sample_size);
  });

  it('refuses entry on a live run that failed closed', () => {
    const r = runCalibration(nowcasts.slice(0, 2), prints.slice(0, 2), {
      ...base,
      seriesProvenance: 'live',
    });
    expect(r.decision).toBe('fail_closed');
    expect(r.entry_eligible).toBe(false);
    expect(r.entry_block_reason).toContain('failed closed');
  });

  it('carries the data-plane attempt log through to the result', () => {
    const r = runCalibration(nowcasts, prints, {
      ...base,
      seriesProvenance: 'live',
      dataPlaneAttempts: [
        {
          source: 'cleveland_nowcast_year_json',
          url: 'https://example.test/nowcast_year.json',
          ok: true,
          status: 200,
          rows: 154,
          error: null,
        },
      ],
    });
    expect(r.data_plane_attempts).toHaveLength(1);
    expect(r.data_plane_attempts[0]?.rows).toBe(154);
  });
});

describe('CLI hermetic path', () => {
  const cli = join(import.meta.dirname, '..', 'src', 'cli', 'pm-desk.ts');
  // Live fetch must be off: drop the opt-in env var entirely so the loader
  // refuses before any network call.
  const env = Object.fromEntries(
    Object.entries(process.env).filter(([k]) => k !== 'PM_DESK_LIVE_CPI'),
  );

  function runCli(args: string[]): { status: number; stdout: string; stderr: string } {
    try {
      const stdout = execFileSync(process.execPath, ['--import', 'tsx', cli, ...args], {
        encoding: 'utf8',
        env,
        timeout: 30_000,
      });
      return { status: 0, stdout, stderr: '' };
    } catch (err) {
      const e = err as { status?: number; stdout?: string; stderr?: string; message?: string };
      return {
        status: e.status ?? 1,
        stdout: e.stdout ?? '',
        stderr: e.stderr ?? e.message ?? '',
      };
    }
  }

  it('emits strict-ish JSON with p_bucket and decision', () => {
    const out = runCli([
      'research',
      'cpi-calibrate',
      '--nowcasts',
      join(FIX, 'nowcasts.csv'),
      '--prints',
      join(FIX, 'bls_yoy.csv'),
      '--as-of',
      '2026-07-31',
      '--live-nowcast',
      '3.42',
      '--bucket',
      '3.4',
      '--mid',
      '0.43',
      '--json',
    ]);
    expect(out.status, out.stderr).toBe(0);
    const payload = JSON.parse(out.stdout) as {
      paper_only: boolean;
      decision: string;
      p_bucket: number;
    };
    expect(payload.paper_only).toBe(true);
    expect(typeof payload.p_bucket).toBe('number');
    expect(payload.decision).toBe('investigate_long');
  });

  it('labels a CSV-only run as fixture and refuses to let it count as entry evidence', () => {
    const out = runCli([
      'research',
      'cpi-calibrate',
      '--nowcasts',
      join(FIX, 'nowcasts.csv'),
      '--prints',
      join(FIX, 'bls_yoy.csv'),
      '--as-of',
      '2026-07-31',
      '--live-nowcast',
      '3.42',
      '--bucket',
      '3.4',
      '--mid',
      '0.43',
      '--json',
    ]);
    expect(out.status, out.stderr).toBe(0);
    const payload = JSON.parse(out.stdout) as {
      series_provenance: string;
      entry_eligible: boolean;
      entry_block_reason: string | null;
      paired_n: number;
      sample_size: number;
      source_urls: string[];
    };
    expect(payload.series_provenance).toBe('fixture');
    expect(payload.entry_eligible).toBe(false);
    expect(payload.entry_block_reason).toContain('fixture');
    expect(payload.paired_n).toBe(payload.sample_size);
    expect(payload.source_urls).toEqual([]);
  });

  it('fails closed on a tiny fixture and reports the reason in JSON', () => {
    const dir = mkdtempSync(join(tmpdir(), 'cpi-harness-'));
    const tinyNow = join(dir, 'nowcasts.csv');
    writeFileSync(tinyNow, 'ref_month,vintage_date,nowcast\n2024-01,2024-01-15,3.40\n');
    const tinyPrints = join(dir, 'prints.csv');
    writeFileSync(tinyPrints, 'ref_month,release_date,yoy\n2024-01,2024-02-12,3.45\n');

    const out = runCli([
      'research',
      'cpi-calibrate',
      '--nowcasts',
      tinyNow,
      '--prints',
      tinyPrints,
      '--as-of',
      '2026-07-31',
      '--live-nowcast',
      '3.42',
      '--bucket',
      '3.4',
      '--mid',
      '0.43',
      '--json',
      '--min-n',
      '12',
    ]);
    expect(out.status, out.stderr).toBe(0);
    const payload = JSON.parse(out.stdout) as { decision: string; fail_reason: string | null };
    expect(payload.decision).toBe('fail_closed');
    expect(payload.fail_reason).toContain('min_n');
  });

  it('live fetch is gated behind PM_DESK_LIVE_CPI=1', () => {
    const out = runCli([
      'research',
      'cpi-calibrate',
      '--nowcasts',
      join(FIX, 'nowcasts.csv'),
      '--prints',
      join(FIX, 'bls_yoy.csv'),
      '--as-of',
      '2026-07-31',
      '--live-nowcast',
      '3.42',
      '--bucket',
      '3.4',
      '--fetch-bls',
      '--json',
    ]);
    // Without the opt-in env var the loader refuses before any network call.
    expect(out.status).not.toBe(0);
    expect(out.stderr).toContain('PM_DESK_LIVE_CPI=1');
  });
});
