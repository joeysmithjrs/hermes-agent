import type { IsoTimestamp } from '../core/time.js';
import type { SLIPPAGE_RULES } from '../schema/adjudication.js';

export type SlippageRule = (typeof SLIPPAGE_RULES)[number];

/**
 * A paper ledger entry.
 *
 * `fill_type` and `paper_only` are enforced by CHECK constraints in the schema,
 * not merely by this type: there is no value either column can hold that would
 * let a row be read as a real fill.
 */
export interface PaperLedgerEntry {
  entry_id: string;
  signal_id: string | null;
  origin: 'adjudication' | 'manual_operator';
  thesis: string;
  thesis_hash: string;
  decided_at: IsoTimestamp;
  market_id: string | null;
  condition_id: string | null;
  token_id: string | null;
  outcome: 'YES' | 'NO' | null;
  entry_observed_at: IsoTimestamp | null;
  entry_mid: number | null;
  entry_best_bid: number | null;
  entry_best_ask: number | null;
  entry_spread: number | null;
  assumed_size_usd: number;
  slippage_rule: SlippageRule;
  /** Derived from the entry observation under `slippage_rule`. Never a fill. */
  assumed_entry_price: number | null;
  expiry_horizon_s: number;
  expires_at: IsoTimestamp;
  markout_horizons_s: number[];
  invalidations: string[];
  evidence_refs: string[];
  paper_only: true;
  fill_type: 'SIMULATED_NO_FILL';
  created_at: IsoTimestamp;
}

export type AnnotationKind = 'markout' | 'outcome' | 'note' | 'invalidated';

export interface LedgerAnnotationInput {
  entry_id: string;
  recorded_at: IsoTimestamp;
  kind: AnnotationKind;
  note?: string;
  detail?: unknown;
}

export interface LedgerAnnotation extends Omit<LedgerAnnotationInput, 'detail'> {
  id: number;
  detail: unknown;
}
