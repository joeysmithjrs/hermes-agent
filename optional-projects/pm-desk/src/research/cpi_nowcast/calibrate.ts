/**
 * Calibrate the residual distribution into bucket probabilities.
 *
 * Given a live nowcast `L` and a set of historical residuals `r_i = print_i -
 * nowcast_i`, each residual implies a hypothetical print `L + r_i`. We round
 * that implied print to one decimal (the contract display) and tally the mass
 * that lands in each 0.1% bucket. So:
 *
 *   P(bucket B | L) = #{ i : round1(L + r_i) == B } / N
 *
 * This is an empirical bootstrap: it is non-parametric, makes no Gaussian
 * assumption, and inherits whatever skew the historical nowcast error actually
 * had. The `p_bucket` returned for a chosen bucket is that mass; nearby buckets
 * are returned too so callers can sanity-check that they sum to ~1 across the
 * support (the union of all implied-print buckets always sums to exactly 1).
 */

import { bucketLabel, roundToOneDecimal } from './round.js';
import type { BucketProb, ResidualDiagnostics } from './types.js';

export interface CalibrateOptions {
  /** The live nowcast used as the shift anchor, e.g. `3.42`. */
  liveNowcast: number;
  /**
   * If true, also report buckets adjacent to the target bucket in `bucket_probs`
   * so the caller can verify probabilities sum to ~1. The target bucket is
   * always included regardless. Defaults to `true`.
   */
  includeNeighbors?: boolean;
  /**
   * How many buckets on each side of the target to include when `includeNeighbors`
   * is set. Defaults to 1 (target ±0.1). Purely cosmetic for the reported map;
   * the normalisation is over the *full* empirical support, not the window.
   */
  neighborBand?: number;
}

/**
 * Compute per-bucket probability mass by bootstrapping the residual distribution
 * against `liveNowcast`. Returns the full empirical distribution keyed by bucket
 * label.
 */
export function bucketProbabilities(
  diagnostics: ResidualDiagnostics,
  liveNowcast: number,
): Map<string, BucketProb> {
  const dist = new Map<string, BucketProb>();
  const residuals = diagnostics.residuals;
  const n = residuals.length;
  if (n === 0) return dist;

  for (const r of residuals) {
    const impliedPrint = roundToOneDecimal(liveNowcast + r);
    const label = bucketLabel(impliedPrint);
    const existing = dist.get(label);
    if (existing) existing.probability += 1 / n;
    else dist.set(label, { bucket: impliedPrint, probability: 1 / n });
  }
  return dist;
}

/**
 * Probability for a single target bucket, plus a small window of neighbors so
 * the JSON consumer can confirm nearby buckets sum to roughly 1. If the residual
 * distribution is empty, every probability is 0.
 */
export function calibrateBucket(
  diagnostics: ResidualDiagnostics,
  targetBucket: number,
  options: CalibrateOptions,
): {
  pBucket: number;
  bucketProbs: Record<string, number>;
} {
  const { includeNeighbors = true, neighborBand = 1 } = options;
  const dist = bucketProbabilities(diagnostics, options.liveNowcast);
  const targetLabel = bucketLabel(targetBucket);
  const target = dist.get(targetLabel);
  const pBucket = target ? target.probability : 0;

  const bucketProbs: Record<string, number> = {};
  if (includeNeighbors) {
    const center = roundToOneDecimal(targetBucket);
    for (let off = -neighborBand; off <= neighborBand; off += 1) {
      const label = (center + off / 10).toFixed(1);
      bucketProbs[label] = dist.get(label)?.probability ?? 0;
    }
  } else {
    bucketProbs[targetLabel] = pBucket;
  }
  return { pBucket, bucketProbs };
}
