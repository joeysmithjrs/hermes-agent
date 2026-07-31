import { randomBytes } from 'node:crypto';

import { sha256Hex } from './hash.js';

/**
 * Deterministic ids let a monitor re-run over the same stored observations and
 * produce byte-identical signal ids, which is what makes ingress idempotency and
 * `--dry-run` repeatability work. Parts are length-prefixed so that
 * `['ab','c']` and `['a','bc']` can never collide.
 */
export function deterministicId(prefix: string, parts: readonly string[]): string {
  const payload = parts.map((part) => `${part.length}:${part}`).join('|');
  return `${prefix}_${sha256Hex(payload).slice(0, 32)}`;
}

/** Non-deterministic id for rows whose identity is their own creation event. */
export function randomId(prefix: string): string {
  return `${prefix}_${randomBytes(12).toString('hex')}`;
}
