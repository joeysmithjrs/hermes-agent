/**
 * Compare the model's bucket probability to a Polymarket mid and emit a
 * research-only decision label. This is an edge *detector*, never an order: the
 * label names a direction to investigate, not a size to place.
 *
 * Convention: a "long" the bucket means the model thinks the bucket is MORE
 * likely than the market mid prices it (p > mid). "Short" means the market
 * over-values the bucket relative to the model (mid > p). An edge survives only
 * if it clears the half-spread plus a model haircut, so trivially small
 * discrepancies are never mislabelled as edges.
 */

import type { Buffer, Decision } from './types.js';

export interface CompareInput {
  pBucket: number;
  mid: number | null;
  buffer: Buffer;
  failClosed: boolean;
  failReason: string | null;
}

export interface CompareResult {
  edgeVsMid: number | null;
  decision: Decision;
  failReason: string | null;
}

/**
 * Threshold (p - mid) must clear (halfSpread + modelHaircut) in absolute terms
 * before any investigate label is emitted. Otherwise no_trade.
 */
export function compareEdge(input: CompareInput): CompareResult {
  if (input.failClosed) {
    return {
      edgeVsMid: null,
      decision: 'fail_closed',
      failReason: input.failReason ?? 'fail_closed',
    };
  }
  if (input.mid === null || !Number.isFinite(input.mid)) {
    // No market mid supplied → research-only probability report, nothing to edge.
    return { edgeVsMid: null, decision: 'no_trade', failReason: null };
  }
  const edge = input.pBucket - input.mid;
  const threshold = input.buffer.halfSpread + input.buffer.modelHaircut;
  if (edge > threshold) {
    return { edgeVsMid: edge, decision: 'investigate_long', failReason: null };
  }
  if (edge < -threshold) {
    return { edgeVsMid: edge, decision: 'investigate_short', failReason: null };
  }
  return { edgeVsMid: edge, decision: 'no_trade', failReason: null };
}
