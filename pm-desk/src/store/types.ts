import type { IsoTimestamp } from '../core/time.js';

/** Public row types. These are the desk's persistence contract. */

export interface MarketUpsert {
  market_id: string;
  condition_id?: string | null;
  question: string;
  slug?: string | null;
  category?: string | null;
  active: boolean;
  closed: boolean;
  accepting_orders: boolean;
  neg_risk: boolean;
  start_date?: IsoTimestamp | null;
  end_date?: IsoTimestamp | null;
  resolution_source?: string | null;
  event_id?: string | null;
  event_title?: string | null;
  observed_at: IsoTimestamp;
  raw: unknown;
}

export interface MarketRow {
  market_id: string;
  condition_id: string | null;
  question: string;
  slug: string | null;
  category: string | null;
  active: boolean;
  closed: boolean;
  accepting_orders: boolean;
  neg_risk: boolean;
  start_date: IsoTimestamp | null;
  end_date: IsoTimestamp | null;
  resolution_source: string | null;
  event_id: string | null;
  event_title: string | null;
  first_seen_at: IsoTimestamp;
  updated_at: IsoTimestamp;
}

export type Outcome = 'YES' | 'NO';

export interface TokenUpsert {
  token_id: string;
  market_id: string;
  outcome: Outcome;
  label?: string | null;
  observed_at: IsoTimestamp;
}

export interface TokenRow {
  token_id: string;
  market_id: string;
  outcome: Outcome;
  label: string | null;
  updated_at: IsoTimestamp;
}

/** A token plus the market context a signal needs to be self-describing. */
export interface TokenBinding {
  token_id: string;
  market_id: string;
  condition_id: string | null;
  outcome: Outcome;
  question: string;
  end_date: IsoTimestamp | null;
  active: boolean;
  closed: boolean;
}

export interface ObservationInput {
  token_id: string;
  market_id?: string | null;
  observed_at: IsoTimestamp;
  mid?: number | null;
  best_bid?: number | null;
  best_ask?: number | null;
  spread?: number | null;
  last_trade_price?: number | null;
  book_available?: boolean;
  bid_depth?: number | null;
  ask_depth?: number | null;
  source?: string;
  raw?: unknown;
}

export interface ObservationRow {
  id: number;
  token_id: string;
  market_id: string | null;
  observed_at: IsoTimestamp;
  mid: number | null;
  best_bid: number | null;
  best_ask: number | null;
  spread: number | null;
  last_trade_price: number | null;
  book_available: boolean;
  bid_depth: number | null;
  ask_depth: number | null;
  source: string;
}

export interface SnapshotInput {
  source_id: string;
  spec_version: number;
  url: string;
  collected_at: IsoTimestamp;
  normalized_text: string;
  content_hash: string;
  raw_artifact_ref?: string | null;
  fields?: Record<string, string | null>;
  collector?: string;
  mode?: 'live' | 'fixture' | 'dry-run';
}

export interface SnapshotRow {
  id: number;
  source_id: string;
  spec_version: number;
  url: string;
  collected_at: IsoTimestamp;
  content_hash: string;
  previous_hash: string | null;
  changed: boolean;
  normalized_artifact_ref: string;
  raw_artifact_ref: string | null;
  fields: Record<string, string | null>;
  collector: string;
  mode: string;
}

export type MonitorOutcome =
  | 'silent'
  | 'emitted'
  | 'suppressed_cooldown'
  | 'suppressed_duplicate'
  | 'skipped_stale'
  | 'skipped_missing_data'
  | 'skipped_disabled';

export interface MonitorDecisionInput {
  rule_id: string;
  rule_version: string;
  evaluated_at: IsoTimestamp;
  outcome: MonitorOutcome;
  dedupe_key?: string;
  signal_id?: string;
  reason?: string;
  detail?: unknown;
}

export interface MonitorDecisionRow extends Omit<MonitorDecisionInput, 'detail'> {
  id: number;
  detail: unknown;
}

export interface MonitorStateRow {
  rule_id: string;
  dedupe_key: string;
  last_emitted_at: IsoTimestamp;
  last_signal_id: string | null;
  last_value: unknown;
}

export type OutboxStatus = 'queued' | 'dispatched' | 'failed';

export interface OutboxRow {
  id: number;
  signal_id: string;
  queued_at: IsoTimestamp;
  status: OutboxStatus;
  dispatcher: string;
  artifact_ref: string | null;
  detail: unknown;
}
