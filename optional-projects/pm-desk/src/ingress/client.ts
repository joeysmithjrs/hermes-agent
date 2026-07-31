import { ConfigError } from '../core/errors.js';
import { nowIso } from '../core/time.js';
import { parseSignalEnvelope, type SignalEnvelope } from '../schema/signal.js';
import { SIGNATURE_HEADER, signPayload, TIMESTAMP_HEADER } from './hmac.js';

export interface SubmitOptions {
  url: string;
  secret: string;
  timeoutMs?: number;
}

export interface SubmitResult {
  status: number;
  body: Record<string, unknown>;
}

/**
 * Test/operator client for the local ingress. Validates the envelope locally
 * first, so a malformed payload fails at the keyboard rather than over HTTP.
 */
export async function submitSignal(
  signal: SignalEnvelope | unknown,
  options: SubmitOptions,
): Promise<SubmitResult> {
  if (!options.secret) {
    throw new ConfigError('PM_DESK_INGRESS_SECRET is not set', {
      hint: 'Export the same secret the server was started with. See .env.example.',
    });
  }

  const envelope = parseSignalEnvelope(signal);
  const body = JSON.stringify(envelope);
  const timestamp = nowIso();

  const response = await fetch(`${options.url.replace(/\/$/, '')}/signals`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      [TIMESTAMP_HEADER]: timestamp,
      [SIGNATURE_HEADER]: signPayload(options.secret, timestamp, body),
    },
    body,
    signal: AbortSignal.timeout(options.timeoutMs ?? 15_000),
  });

  const text = await response.text();
  return {
    status: response.status,
    body: text ? (JSON.parse(text) as Record<string, unknown>) : {},
  };
}
