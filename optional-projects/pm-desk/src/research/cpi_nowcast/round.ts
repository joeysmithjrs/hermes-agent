/**
 * One-decimal rounding that matches how a BLS-style CPI contract displays its
 * headline print: the YoY % is shown to a single decimal, so the contract pays
 * on the rounded value, not the raw one.
 *
 * RULE — half-up at the second decimal (round half away from zero):
 *   3.34 → 3.3
 *   3.35 → 3.4
 *   3.44 → 3.4
 *   3.45 → 3.5
 *
 * Why not just `Math.round(x * 10) / 10`: binary floats cannot represent most
 * decimal tenths exactly, so a value stored as `3.35` is internally
 * `3.3500000000000000882...` and `x * 10` is `33.5` (good here), but other
 * inputs land at `x.xxx999...` and the naive path silently rounds the wrong
 * way. We add a tiny epsilon (1e-9, scaled) before the floor so a true half
 * rounds up without dragging a value that is genuinely below the half over the
 * line — 1e-9 in scaled space is 1e-10 in the original, far smaller than any
 * real nowcast precision.
 */

/**
 * Round a YoY % to one decimal, half-up. Returns the numeric bucket center
 * (e.g. `3.4`). Safe for the negative residuals case (rounds half away from
 * zero) even though we only ever round positive implied prints.
 */
export function roundToOneDecimal(value: number): number {
  if (!Number.isFinite(value)) {
    throw new RangeError(`roundToOneDecimal: value must be finite, got ${value}`);
  }
  const sign = value < 0 ? -1 : 1;
  const magnitude = Math.abs(value) * 10;
  const tenths = Math.floor(magnitude + 0.5 + 1e-9);
  return (sign * tenths) / 10;
}

/**
 * The one-decimal label used as a bucket key, matching the contract display.
 * Uses fixed 1-decimal formatting so `3.0` stays `"3.0"` (not `"3"`).
 */
export function bucketLabel(bucket: number): string {
  return roundToOneDecimal(bucket).toFixed(1);
}
