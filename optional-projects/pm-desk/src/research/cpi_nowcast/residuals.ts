/**
 * Join nowcasts to actual BLS prints and compute residuals (`print - nowcast`),
 * enforcing the no-look-ahead invariant: a nowcast's vintage must be strictly
 * before the print's release. Any pair that would peek at the future is dropped,
 * never silently used.
 */

import type { NowcastRow, PrintRow, ResidualDiagnostics, ResidualPoint } from './types.js';

/**
 * Pair nowcasts to prints by `refMonth`, keeping only pairs where the nowcast
 * vintage strictly precedes the print release. If a reference month has both a
 * later nowcast and an earlier one, the *latest* no-look-ahead nowcast wins — it
 * is the best information available before the print dropped.
 */
export function joinResiduals(
  nowcasts: readonly NowcastRow[],
  prints: readonly PrintRow[],
): { points: ResidualPoint[]; droppedLookahead: number } {
  const printByMonth = new Map<string, PrintRow>();
  for (const p of prints) printByMonth.set(p.refMonth, p);

  // Group nowcasts by refMonth so we can pick the latest vintage per month.
  const byMonth = new Map<string, NowcastRow[]>();
  for (const n of nowcasts) {
    const list = byMonth.get(n.refMonth);
    if (list) list.push(n);
    else byMonth.set(n.refMonth, [n]);
  }

  const points: ResidualPoint[] = [];
  let droppedLookahead = 0;

  for (const [refMonth, vintages] of byMonth) {
    const print = printByMonth.get(refMonth);
    if (print === undefined) continue; // no print yet → can't form a residual

    // Latest vintage that still respects no-look-ahead.
    const eligible = vintages
      .filter((n) => n.vintageDate.getTime() < print.releaseDate.getTime())
      .sort((a, b) => a.vintageDate.getTime() - b.vintageDate.getTime());
    const lookaheadThisMonth = vintages.length - eligible.length;
    droppedLookahead += lookaheadThisMonth;
    if (eligible.length === 0) continue;

    const nowcast = eligible[eligible.length - 1]!;
    points.push({
      refMonth,
      nowcast: nowcast.nowcast,
      print: print.yoy,
      residual: print.yoy - nowcast.nowcast,
      vintageDate: nowcast.vintageDate,
      releaseDate: print.releaseDate,
    });
  }

  // Stable: order by reference month so residuals are reproducible.
  points.sort((a, b) => (a.refMonth < b.refMonth ? -1 : a.refMonth > b.refMonth ? 1 : 0));
  return { points, droppedLookahead };
}

/** RMSE of the residuals (root mean squared error of nowcast vs print). */
export function computeDiagnostics(points: readonly ResidualPoint[]): ResidualDiagnostics {
  const residuals = points.map((p) => p.residual);
  const n = residuals.length;
  if (n === 0) {
    return {
      sampleSize: 0,
      residualMean: 0,
      residualRmse: 0,
      residuals: [],
      droppedLookahead: 0,
    };
  }
  let sum = 0;
  let sqSum = 0;
  for (const r of residuals) {
    sum += r;
    sqSum += r * r;
  }
  const mean = sum / n;
  const rmse = Math.sqrt(sqSum / n);
  return {
    sampleSize: n,
    residualMean: mean,
    residualRmse: rmse,
    residuals,
    droppedLookahead: 0,
  };
}
