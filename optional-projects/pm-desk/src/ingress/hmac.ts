import { createHmac, timingSafeEqual } from 'node:crypto';

import { systemClock, type Clock } from '../core/time.js';

/**
 * Request authentication for the local ingress.
 *
 * The signature covers `timestamp.body`, not just the body, so a captured
 * request cannot be replayed outside a short window even though the payload is
 * unchanged. Comparison is constant-time, and no failure path ever includes the
 * secret or the expected digest in its output.
 */
export const SIGNATURE_HEADER = 'x-pm-desk-signature';
export const TIMESTAMP_HEADER = 'x-pm-desk-timestamp';
const DEFAULT_TOLERANCE_SECONDS = 300;

export function signPayload(secret: string, timestamp: string, body: string): string {
  const digest = createHmac('sha256', secret).update(`${timestamp}.${body}`).digest('hex');
  return `sha256=${digest}`;
}

export interface VerifyOptions {
  clock?: Clock;
  toleranceSeconds?: number;
}

export interface VerifyResult {
  ok: boolean;
  /** Safe to log and return to the caller: never contains secret material. */
  reason?: string;
}

export function verifySignature(
  secret: string,
  timestamp: string | undefined,
  body: string,
  provided: string | undefined,
  options: VerifyOptions = {},
): VerifyResult {
  if (!provided) return { ok: false, reason: `missing ${SIGNATURE_HEADER} header` };
  if (!timestamp) return { ok: false, reason: `missing ${TIMESTAMP_HEADER} header` };

  const parsed = Date.parse(timestamp);
  if (Number.isNaN(parsed)) {
    return { ok: false, reason: `${TIMESTAMP_HEADER} is not a valid ISO-8601 timestamp` };
  }

  const clock = options.clock ?? systemClock;
  const tolerance = options.toleranceSeconds ?? DEFAULT_TOLERANCE_SECONDS;
  const skew = Math.abs(Date.parse(clock.now()) - parsed) / 1000;
  if (skew > tolerance) {
    return {
      ok: false,
      reason: `${TIMESTAMP_HEADER} is outside the ${tolerance}s replay window (skew ${Math.round(skew)}s)`,
    };
  }

  const expected = Buffer.from(signPayload(secret, timestamp, body));
  const actual = Buffer.from(provided);
  if (expected.length !== actual.length) {
    return { ok: false, reason: 'signature mismatch' };
  }
  if (!timingSafeEqual(expected, actual)) {
    return { ok: false, reason: 'signature mismatch' };
  }
  return { ok: true };
}
