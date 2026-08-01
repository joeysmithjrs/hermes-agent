/**
 * `pm-desk research` — paper-only research harnesses.
 *
 * Currently hosts the CPI nowcast-to-bucket calibration harness, which converts
 * historical nowcast error into P(BLS CPI print lands in a one-decimal contract
 * bucket) and compares that to a Polymarket mid. The decision label is research
 * only and is never an order.
 */

import { UsageError } from '../../core/errors.js';
import {
  fetchBlsLadder,
  fetchClevelandLadder,
  loadNowcastsCsv,
  loadPrintsCsv,
} from '../../research/cpi_nowcast/loaders/index.js';
import { runCalibration } from '../../research/cpi_nowcast/index.js';
import type {
  CalibrationResult,
  DataPlaneAttempt,
  NowcastRow,
  PrintRow,
  SeriesProvenance,
} from '../../research/cpi_nowcast/types.js';
import type { Flags } from '../args.js';
import { emit } from '../output.js';

const CPI_HELP = `pm-desk research cpi-calibrate — PAPER-ONLY CPI nowcast calibration

Converts historical nowcast error (nowcast vs actual BLS print) into the
probability that the next BLS CPI YoY print rounds into a one-decimal contract
bucket, then optionally compares that to a Polymarket mid.

USAGE
  pm-desk research cpi-calibrate \\
    --nowcasts <csv> --prints <csv> \\
    --live-nowcast 3.42 --bucket 3.4 \\
    [--mid 0.43] [--min-n 12] [--half-spread 0.01] [--model-haircut 0.05] \\
    [--json]

INPUTS
  --nowcasts <csv>   CSV with columns: ref_month, vintage_date, nowcast
  --prints   <csv>   CSV with columns: ref_month, release_date, yoy
  --live-nowcast     The live nowcast used as the calibration anchor (e.g. 3.42)
  --bucket           The one-decimal contract bucket to score (e.g. 3.4)
  --as-of YYYY-MM-DD Accepted for compatibility; unused (the live feed is self-dated)

OPTIONAL MARKET + THRESHOLDS
  --mid              Polymarket mid for the bucket contract (skip for model-only)
  --min-n            Minimum no-look-ahead pairs; below this → fail_closed (default 12)
  --half-spread      Half-spread buffer vs the mid (default 0.01)
  --model-haircut    Model-haircut buffer vs the mid (default 0.05)

LIVE FETCH — boil-the-ocean ladder (opt-in; never run in hermetic tests; no
credentials; every rung recorded in data_plane_attempts win or lose)
  --fetch-cleveland  Nowcasts (~150 reference months) + prints:
                       1. nowcast_year.json feed (primary; the page itself
                          renders from this)
                       2. HTML page scrape, current month only (best-effort
                          fallback; the page is client-rendered from #1, so
                          this rung legitimately may find nothing)
                     Needs PM_DESK_LIVE_CPI=1.
  --fetch-bls        Prints fallback: BLS public API, reached when the
                     Cleveland feed's own Actual-print series is empty for a
                     reference month (BLS has not released it yet).
                     Needs PM_DESK_LIVE_CPI=1.

PROVENANCE
  Every result carries series_provenance (fixture | live | mixed) and
  entry_eligible. A fixture or mixed run is RESEARCH ONLY: it can never justify
  packaging monitors or a paper entry, however good its numbers look.
  --allow-mixed-entry  Human override letting a mixed run count as entry
                       evidence. Not for agents to set on their own.

OUTPUT
  --json             Emit the strict-ish CalibrationResult JSON

PAPER ONLY. This command cannot trade, sign, place an order or touch a wallet.
`;

export async function researchCommand(sub: string | undefined, flags: Flags): Promise<number> {
  switch (sub) {
    case 'cpi-calibrate':
      return cpiCalibrate(flags);
    default:
      throw new UsageError(`unknown \`research\` subcommand: ${sub ?? '(none)'}`, {
        hint: 'Try: pm-desk research cpi-calibrate --help',
      });
  }
}

async function cpiCalibrate(flags: Flags): Promise<number> {
  if (flags.bool('help')) {
    process.stdout.write(`${CPI_HELP}\n`);
    return 0;
  }

  const json = flags.bool('json');
  const nowcastPath = flags.str('nowcasts');
  const printPath = flags.str('prints');
  const liveNowcastRaw = flags.str('live-nowcast');
  const bucketRaw = flags.str('bucket');
  const midRaw = flags.str('mid');
  flags.str('as-of'); // accepted for compatibility; the live ladder is self-dated by the feed
  const minN = flags.int('min-n', 12)!;
  const halfSpread = Number(flags.str('half-spread', '0.01')!);
  const modelHaircut = Number(flags.str('model-haircut', '0.05')!);
  const fetchCleveland = flags.bool('fetch-cleveland');
  const fetchBls = flags.bool('fetch-bls');
  const allowMixedEntry = flags.bool('allow-mixed-entry');
  flags.rejectUnknown('research cpi-calibrate');

  if (liveNowcastRaw === undefined) {
    throw new UsageError('missing required flag --live-nowcast', {
      hint: 'Pass the live nowcast anchor, e.g. --live-nowcast 3.42.',
    });
  }
  const liveNowcast = Number(liveNowcastRaw);
  if (!Number.isFinite(liveNowcast)) {
    throw new UsageError(`--live-nowcast must be a number, got ${JSON.stringify(liveNowcastRaw)}`, {
      hint: 'Example: --live-nowcast 3.42',
    });
  }
  if (bucketRaw === undefined) {
    throw new UsageError('missing required flag --bucket', {
      hint: 'Pass the one-decimal bucket to score, e.g. --bucket 3.4.',
    });
  }
  const bucket = Number(bucketRaw);
  if (!Number.isFinite(bucket)) {
    throw new UsageError(`--bucket must be a number, got ${JSON.stringify(bucketRaw)}`, {
      hint: 'Example: --bucket 3.4',
    });
  }
  if (!Number.isFinite(halfSpread) || halfSpread < 0) {
    throw new UsageError('--half-spread must be a non-negative number', {
      hint: 'Example: --half-spread 0.01',
    });
  }
  if (!Number.isFinite(modelHaircut) || modelHaircut < 0) {
    throw new UsageError('--model-haircut must be a non-negative number', {
      hint: 'Example: --model-haircut 0.05',
    });
  }

  let mid: number | null = null;
  if (midRaw !== undefined) {
    mid = Number(midRaw);
    if (!Number.isFinite(mid) || mid < 0 || mid > 1) {
      throw new UsageError('--mid must be a probability in [0,1]', {
        hint: 'Example: --mid 0.43',
      });
    }
  }

  const notes: string[] = [];
  const sourceUrls: string[] = [];
  const dataPlaneAttempts: DataPlaneAttempt[] = [];

  // Load fixture nowcasts/prints, then optionally overlay live rows. Which of
  // those two happened is what `series_provenance` reports, so track it as we go
  // rather than inferring it from the flags afterwards: a --fetch-* that
  // returned nothing usable is a fixture run, whatever the caller asked for.
  let nowcasts: NowcastRow[] = [];
  let prints: PrintRow[] = [];
  let nowcastsFromFixture = false;
  let nowcastsFromLive = false;
  let printsFromFixture = false;
  let printsFromLive = false;
  if (nowcastPath) {
    nowcasts = loadNowcastsCsv(nowcastPath);
    nowcastsFromFixture = nowcasts.length > 0;
  }
  if (printPath) {
    prints = loadPrintsCsv(printPath);
    printsFromFixture = prints.length > 0;
  }

  if (fetchCleveland) {
    // Boil-the-ocean ladder: the JSON feed the nowcasting page itself renders
    // from (rung 1, ~150 reference months), falling back to a best-effort HTML
    // scrape of the page for the current month only (rung 2). Every rung is
    // recorded whether it worked or not — a single failed request is never
    // treated as an exhausted ladder.
    const ladder = await fetchClevelandLadder();
    dataPlaneAttempts.push(...ladder.attempts);
    for (const attempt of ladder.attempts) if (attempt.ok) sourceUrls.push(attempt.url);
    if (ladder.nowcasts.length > 0) {
      notes.push(`using ${ladder.nowcasts.length} live Cleveland nowcast row(s)`);
      nowcastsFromLive = true;
      nowcasts = [...nowcasts, ...ladder.nowcasts];
    }
    if (ladder.prints.length > 0) {
      notes.push(`using ${ladder.prints.length} live Cleveland print row(s) (Actual CPI Inflation)`);
      printsFromLive = true;
      prints = [...prints, ...ladder.prints];
    }
  }
  if (fetchBls) {
    // Fallback rung for prints: only reached when the Cleveland feed's own
    // `Actual CPI Inflation` series has not been populated for a reference
    // month yet (BLS has not released), or the caller wants BLS regardless.
    const ladder = await fetchBlsLadder();
    dataPlaneAttempts.push(...ladder.attempts);
    for (const attempt of ladder.attempts) if (attempt.ok) sourceUrls.push(attempt.url);
    if (ladder.prints.length > 0) {
      notes.push(`using ${ladder.prints.length} live BLS print row(s)`);
      printsFromLive = true;
      // De-dup by refMonth, preferring the live row already collected (Cleveland
      // over BLS, since it was tried first) unless BLS is the only one that had
      // this reference month at all.
      const map = new Map<string, PrintRow>();
      for (const p of [...ladder.prints, ...prints]) map.set(p.refMonth, p);
      prints = [...map.values()];
    }
  }

  if (nowcasts.length === 0) {
    throw new UsageError('no nowcast rows available after exhausting the ladder', {
      hint: `Pass --nowcasts <csv>, or --fetch-cleveland with PM_DESK_LIVE_CPI=1.${attemptsHint(dataPlaneAttempts)}`,
    });
  }
  if (prints.length === 0) {
    throw new UsageError('no print rows available after exhausting the ladder', {
      hint: `Pass --prints <csv>, or --fetch-cleveland / --fetch-bls with PM_DESK_LIVE_CPI=1.${attemptsHint(dataPlaneAttempts)}`,
    });
  }

  const result = runCalibration(nowcasts, prints, {
    liveNowcast,
    bucket,
    mid,
    minN,
    buffer: { halfSpread, modelHaircut },
    seriesProvenance: classifyProvenance({
      nowcastsFromFixture,
      nowcastsFromLive,
      printsFromFixture,
      printsFromLive,
    }),
    sourceUrls,
    dataPlaneAttempts,
    allowMixedEntry,
    notes,
  });

  emit({ json }, result, () => humanSummary(result));
  return 0;
}

/**
 * Render every ladder rung tried, success or failure, as part of a usage-error
 * hint. "The data was unavailable" must come with the receipts: which URLs
 * were hit, their status, and why each one came up empty — not a claim taken
 * on faith after a single request.
 */
function attemptsHint(attempts: readonly DataPlaneAttempt[]): string {
  if (attempts.length === 0) return '';
  const lines = attempts.map(
    (a) => `\n  [${a.ok ? 'ok' : 'fail'}] ${a.source} ${a.url} status=${a.status ?? '-'} ${a.ok ? `rows=${a.rows}` : (a.error ?? '')}`,
  );
  return ` Ladder attempts:${lines.join('')}`;
}

/**
 * `live` only when both series came off a public endpoint this run and neither
 * was topped up from disk. Anything else is `mixed`, and disk-only is `fixture`.
 */
function classifyProvenance(flags: {
  nowcastsFromFixture: boolean;
  nowcastsFromLive: boolean;
  printsFromFixture: boolean;
  printsFromLive: boolean;
}): SeriesProvenance {
  const anyFixture = flags.nowcastsFromFixture || flags.printsFromFixture;
  const anyLive = flags.nowcastsFromLive || flags.printsFromLive;
  if (anyLive && !anyFixture) return 'live';
  if (anyLive && anyFixture) return 'mixed';
  return 'fixture';
}

function humanSummary(r: CalibrationResult): string {
  const lines = [
    '[PAPER ONLY] CPI nowcast → bucket calibration',
    // Provenance goes first, above the numbers, because the numbers read the
    // same either way and the reader needs to know which kind they are looking
    // at before the p_bucket lands.
    `  provenance       ${r.series_provenance}${r.entry_eligible ? '' : '  ← RESEARCH ONLY'}`,
    `  sample_size      ${r.sample_size} (min_n 12)`,
    `  residual mean    ${r.residual_mean.toFixed(3)}  rmse ${r.residual_rmse.toFixed(3)}`,
    `  live_nowcast     ${r.live_nowcast}  →  bucket ${r.bucket}`,
    `  P(bucket)        ${r.p_bucket}`,
    `  bucket_probs     ${formatProbs(r.bucket_probs)}`,
  ];
  if (r.mid !== null) {
    lines.push(
      `  mid              ${r.mid}  edge_vs_mid ${r.edge_vs_mid}  (buffer ${r.buffer.halfSpread}+${r.buffer.modelHaircut})`,
    );
  }
  lines.push(`  decision         ${r.decision}${r.fail_reason ? `  (${r.fail_reason})` : ''}`);
  lines.push(
    `  entry_eligible   ${r.entry_eligible}${r.entry_block_reason ? `  (${r.entry_block_reason})` : ''}`,
  );
  for (const a of r.data_plane_attempts) {
    lines.push(
      `  attempt          [${a.ok ? 'ok' : 'fail'}] ${a.source} ${a.status ?? '-'} ${a.ok ? `${a.rows} row(s)` : (a.error ?? '')}`,
    );
  }
  for (const u of r.source_urls) lines.push(`  source           ${u}`);
  for (const n of r.notes) lines.push(`  note             ${n}`);
  return lines.join('\n');
}

function formatProbs(probs: Record<string, number>): string {
  return Object.entries(probs)
    .map(([k, v]) => `${k}:${v}`)
    .join('  ');
}
