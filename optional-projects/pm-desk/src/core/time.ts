/**
 * Every timestamp the desk persists or emits is UTC ISO-8601 with millisecond
 * precision (`2026-07-30T12:00:00.000Z`). Epoch millis are used only for arithmetic.
 */
import { ConfigError } from './errors.js';

export type IsoTimestamp = string;

export function nowIso(): IsoTimestamp {
  return new Date().toISOString();
}

export function isoFromEpochMs(ms: number): IsoTimestamp {
  if (!Number.isFinite(ms)) {
    throw new ConfigError(`cannot format non-finite epoch value: ${ms}`, {
      hint: 'Pass a finite millisecond timestamp.',
    });
  }
  return new Date(ms).toISOString();
}

export function parseIsoToEpochMs(iso: string): number {
  const ms = Date.parse(iso);
  if (Number.isNaN(ms)) {
    throw new ConfigError(`unparseable timestamp: ${JSON.stringify(iso)}`, {
      hint: 'Use UTC ISO-8601, e.g. 2026-07-30T12:00:00.000Z.',
    });
  }
  return ms;
}

/** Alias used at call sites that read more naturally as a conversion. */
export const epochMs = parseIsoToEpochMs;

/** Whole seconds between two ISO timestamps (`b - a`), useful for cooldowns. */
export function secondsBetween(a: IsoTimestamp, b: IsoTimestamp): number {
  return (parseIsoToEpochMs(b) - parseIsoToEpochMs(a)) / 1000;
}

/** A clock seam so monitors, ingress and ledger stay deterministic under test. */
export interface Clock {
  now(): IsoTimestamp;
}

export const systemClock: Clock = { now: nowIso };

export function fixedClock(iso: IsoTimestamp): Clock {
  parseIsoToEpochMs(iso);
  return { now: () => iso };
}
