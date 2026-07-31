/**
 * Orchestration: nowcasts + prints → residual diagnostics → bucket probability
 * → edge-vs-mid decision. This is the single pure entry point the CLI calls; it
 * has no I/O of its own and no side effects, so it is what the tests exercise.
 */

import { calibrateBucket } from './calibrate.js';
import { compareEdge } from './compare.js';
import { computeDiagnostics, joinResiduals } from './residuals.js';
import type { Buffer, CalibrationResult, NowcastRow, PrintRow } from './types.js';

export interface RunCalibrationOptions {
  liveNowcast: number;
  bucket: number;
  mid: number | null;
  /** Minimum no-look-ahead residual pairs; below this we fail closed. */
  minN?: number;
  buffer?: Buffer;
  notes?: string[];
}

const DEFAULT_MIN_N = 12;
const DEFAULT_BUFFER: Buffer = { halfSpread: 0.01, modelHaircut: 0.05 };

/**
 * Run the full calibration. Always returns a `CalibrationResult` with
 * `paper_only: true`; never throws on insufficient data — it fails closed
 * instead, so a caller cannot mistake an empty input for a no-trade edge.
 */
export function runCalibration(
  nowcasts: readonly NowcastRow[],
  prints: readonly PrintRow[],
  options: RunCalibrationOptions,
): CalibrationResult {
  const minN = options.minN ?? DEFAULT_MIN_N;
  const buffer = options.buffer ?? DEFAULT_BUFFER;
  const notes = [...(options.notes ?? [])];

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

  return {
    paper_only: true,
    sample_size: diagnostics.sampleSize,
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
    notes,
  };
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
