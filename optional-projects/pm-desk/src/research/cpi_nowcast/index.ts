/**
 * Orchestration: nowcasts + prints → residual diagnostics → bucket probability
 * → edge-vs-mid decision. This is the single pure entry point the CLI calls; it
 * has no I/O of its own and no side effects, so it is what the tests exercise.
 */

import { calibrateBucket } from './calibrate.js';
import { compareEdge } from './compare.js';
import { computeDiagnostics, joinResiduals } from './residuals.js';
import type {
  Buffer,
  CalibrationResult,
  DataPlaneAttempt,
  NowcastRow,
  PrintRow,
  SeriesProvenance,
} from './types.js';

export interface RunCalibrationOptions {
  liveNowcast: number;
  bucket: number;
  mid: number | null;
  /** Minimum no-look-ahead residual pairs; below this we fail closed. */
  minN?: number;
  buffer?: Buffer;
  /**
   * Where the two series came from. Defaults to `fixture`, which is the safe
   * default: a caller that forgets to say has not proven it went live, and the
   * result must not be citable as entry evidence on an omission.
   */
  seriesProvenance?: SeriesProvenance;
  sourceUrls?: string[];
  dataPlaneAttempts?: DataPlaneAttempt[];
  /**
   * Let a `mixed` run count as entry evidence. Reserved for an explicit human
   * decision ("the prints are live, the nowcast history is the checked-in
   * vintage file, I accept that") — never set by an agent on its own.
   */
  allowMixedEntry?: boolean;
  notes?: string[];
}

const DEFAULT_MIN_N = 12;
const DEFAULT_BUFFER: Buffer = { halfSpread: 0.01, modelHaircut: 0.05 };

/**
 * Run the full calibration. Always returns a `CalibrationResult` with
 * `paper_only: true`; never throws on insufficient data — it fails closed
 * instead, so a caller cannot mistake an empty input for a no-trade edge.
 *
 * A result carries `entry_eligible` alongside `decision`, and the two answer
 * different questions. `decision` is what the numbers say. `entry_eligible` is
 * whether the numbers are allowed to mean anything: a fixture run can produce a
 * confident-looking `investigate_long`, and that is exactly the failure this
 * flag exists to stop.
 */
export function runCalibration(
  nowcasts: readonly NowcastRow[],
  prints: readonly PrintRow[],
  options: RunCalibrationOptions,
): CalibrationResult {
  const minN = options.minN ?? DEFAULT_MIN_N;
  const buffer = options.buffer ?? DEFAULT_BUFFER;
  const notes = [...(options.notes ?? [])];
  const provenance = options.seriesProvenance ?? 'fixture';

  const { points, droppedLookahead } = joinResiduals(nowcasts, prints);
  const diagnostics = computeDiagnostics(points);
  if (droppedLookahead > 0) {
    notes.push(`dropped ${droppedLookahead} nowcast(s) that violated no-look-ahead`);
  }

  const { pBucket, bucketProbs } = calibrateBucket(diagnostics, options.bucket, {
    liveNowcast: options.liveNowcast,
  });

  let failReason: string | null = null;
  let failClosed = false;
  if (diagnostics.sampleSize < minN) {
    failClosed = true;
    failReason = `sample_size ${diagnostics.sampleSize} < min_n ${minN}`;
    notes.push(failReason);
  }

  const compare = compareEdge({
    pBucket,
    mid: options.mid,
    buffer,
    failClosed,
    failReason,
  });

  const entryBlockReason = entryBlock(provenance, options.allowMixedEntry ?? false, failClosed);
  if (entryBlockReason !== null) notes.push(`research only: ${entryBlockReason}`);

  return {
    paper_only: true,
    series_provenance: provenance,
    source_urls: [...(options.sourceUrls ?? [])],
    entry_eligible: entryBlockReason === null,
    entry_block_reason: entryBlockReason,
    sample_size: diagnostics.sampleSize,
    paired_n: diagnostics.sampleSize,
    residual_rmse: round3(diagnostics.residualRmse),
    residual_mean: round3(diagnostics.residualMean),
    bucket: options.bucket,
    live_nowcast: options.liveNowcast,
    p_bucket: round4(pBucket),
    bucket_probs: roundMap4(bucketProbs),
    mid: options.mid,
    edge_vs_mid: compare.edgeVsMid === null ? null : round4(compare.edgeVsMid),
    buffer,
    decision: compare.decision,
    fail_reason: compare.failReason,
    data_plane_attempts: [...(options.dataPlaneAttempts ?? [])],
    notes,
  };
}

/**
 * The one rule that keeps a checked-in CSV from becoming a trade signal:
 * anything that touched a fixture, and anything that failed closed, is research
 * only. Returns the reason it is blocked, or null when the result may be cited.
 */
function entryBlock(
  provenance: SeriesProvenance,
  allowMixedEntry: boolean,
  failClosed: boolean,
): string | null {
  if (provenance === 'fixture') {
    return 'series_provenance is fixture — checked-in CSVs cannot justify entry or monitors';
  }
  if (provenance === 'mixed' && !allowMixedEntry) {
    return 'series_provenance is mixed — one series came from a fixture; needs an explicit human override';
  }
  if (failClosed) {
    return 'calibration failed closed — no usable residual sample';
  }
  return null;
}

function round3(x: number): number {
  return Math.round(x * 1000) / 1000;
}
function round4(x: number): number {
  return Math.round(x * 10000) / 10000;
}
function roundMap4(map: Record<string, number>): Record<string, number> {
  const out: Record<string, number> = {};
  for (const [k, v] of Object.entries(map)) out[k] = round4(v);
  return out;
}
