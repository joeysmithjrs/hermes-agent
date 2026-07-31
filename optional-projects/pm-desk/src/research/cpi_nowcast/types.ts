/**
 * CPI nowcast → rounded-bucket calibration harness.

 * PAPER-ONLY RESEARCH. This module and everything it imports is read-only: it
 * turns historical nowcast error into the probability that a BLS CPI print lands
 * in a one-decimal contract bucket, and may compare that to a Polymarket mid.
 * It cannot trade, sign, place an order or touch a wallet — that boundary is
 * enforced structurally by the package guard test, which scans all of `src/`.
 */

/** A nowcast made at a known vintage, for a CPI reference month. */
export interface NowcastRow {
  /** CPI reference month, `YYYY-MM`. */
  refMonth: string;
  /** When the nowcast was made. Must precede the print release (no look-ahead). */
  vintageDate: Date;
  /** The nowcasted YoY %, e.g. `3.42`. */
  nowcast: number;
}

/** The actual BLS print for a CPI reference month. */
export interface PrintRow {
  /** CPI reference month, `YYYY-MM`. */
  refMonth: string;
  /** When the print was released by BLS. */
  releaseDate: Date;
  /** Actual headline YoY %, e.g. `3.0`. */
  yoy: number;
}

/** A joined, no-look-ahead nowcast/print pair with its residual. */
export interface ResidualPoint {
  refMonth: string;
  nowcast: number;
  print: number;
  /** `print - nowcast` (documented sign). */
  residual: number;
  vintageDate: Date;
  releaseDate: Date;
}

/** Summary of the empirical residual distribution. */
export interface ResidualDiagnostics {
  sampleSize: number;
  residualMean: number;
  residualRmse: number;
  /** The residuals themselves, in reference-month order. */
  residuals: number[];
  /** Pairs dropped because the nowcast vintage was not before the release. */
  droppedLookahead: number;
}

/** A 0.1% bucket, keyed by its one-decimal label (e.g. `"3.4"`). */
export interface BucketProb {
  /** The bucket center as a one-decimal number, e.g. `3.4`. */
  bucket: number;
  /** Probability mass landing in this bucket after rounding, in [0,1]. */
  probability: number;
}

/** Buffer against the market mid before any edge is declared. */
export interface Buffer {
  halfSpread: number;
  modelHaircut: number;
}

/** The research-only decision label. NEVER an order. */
export type Decision = 'no_trade' | 'investigate_long' | 'investigate_short' | 'fail_closed';

/** The strict-ish JSON shape emitted by the CLI. */
export interface CalibrationResult {
  paper_only: true;
  sample_size: number;
  residual_rmse: number;
  residual_mean: number;
  bucket: number;
  live_nowcast: number;
  p_bucket: number;
  bucket_probs: Record<string, number>;
  mid: number | null;
  edge_vs_mid: number | null;
  buffer: Buffer;
  decision: Decision;
  fail_reason: string | null;
  notes: string[];
}
