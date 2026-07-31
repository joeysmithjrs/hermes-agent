import type { Clock, IsoTimestamp } from '../core/time.js';
import type { MarketRef, MarketSnapshot, SignalEnvelope, SourceRef } from '../schema/signal.js';
import type { MonitorOutcome } from '../store/types.js';

/**
 * What a rule produces when its predicate fires. The engine — not the rule —
 * owns id derivation, dedupe, cooldown and persistence, so every rule stays a
 * pure predicate over stored data.
 */
export interface RuleCandidate {
  kind: SignalEnvelope['kind'];
  observed_at: IsoTimestamp;
  /** Stable across re-evaluations of the same underlying facts. */
  dedupe_key: string;
  market_refs: MarketRef[];
  source_refs: SourceRef[];
  market_snapshot: MarketSnapshot | null;
  evidence: SignalEnvelope['evidence'];
}

/** A rule either fires with a candidate, or explains why it did not. */
export type RuleResult =
  | { fired: true; candidate: RuleCandidate }
  | {
      fired: false;
      outcome: Exclude<MonitorOutcome, 'emitted' | 'suppressed_cooldown' | 'suppressed_duplicate'>;
      reason: string;
    };

export interface MonitorEvaluation {
  rule_id: string;
  rule_version: string;
  evaluated_at: IsoTimestamp;
  outcome: MonitorOutcome;
  reason?: string;
  /** Present only when `outcome === 'emitted'`. Always schema-valid. */
  signal?: SignalEnvelope;
  dryRun: boolean;
}

export interface EvaluateOptions {
  clock?: Clock;
  /** Evaluate and report without writing signals, state or audit rows. */
  dryRun?: boolean;
}

export function silent(reason: string): RuleResult {
  return { fired: false, outcome: 'silent', reason };
}

export function missingData(reason: string): RuleResult {
  return { fired: false, outcome: 'skipped_missing_data', reason };
}

export function stale(reason: string): RuleResult {
  return { fired: false, outcome: 'skipped_stale', reason };
}
