import { z } from 'zod';

import { validate } from './common.js';

/**
 * The adjudication workflow's contract. The decision set is closed at three
 * values and there is no field anywhere in this schema that could express an
 * order, a size in shares, a venue action or a wallet. A `paper_alert` may carry
 * a ledger proposal — a *proposal*, which the ledger layer re-validates before
 * it will write anything.
 */
export const ADJUDICATION_VERSION = 1 as const;

export const AdjudicationDecisionSchema = z.enum(['ignore', 'watch', 'paper_alert']);
export type AdjudicationDecision = z.infer<typeof AdjudicationDecisionSchema>;

export const SLIPPAGE_RULES = ['cross_spread_full', 'mid_plus_1_tick', 'mid_no_slippage'] as const;

export const LedgerProposalSchema = z
  .object({
    thesis: z.string().min(10).max(2000),
    candidate_outcome: z.enum(['YES', 'NO']),
    /** Notional in USD. Paper only — never sent anywhere. */
    assumed_size_usd: z.number().gt(0).max(100_000),
    slippage_rule: z.enum(SLIPPAGE_RULES),
    expiry_horizon_s: z
      .number()
      .int()
      .positive()
      .max(86_400 * 365),
    markout_horizons_s: z.array(z.number().int().positive()).min(1).max(6),
    invalidations: z.array(z.string().min(1)).min(1).max(10),
  })
  .strict();

export type LedgerProposal = z.infer<typeof LedgerProposalSchema>;

export const AdjudicationSchema = z
  .object({
    version: z.literal(ADJUDICATION_VERSION),
    signal_id: z.string().regex(/^sig_[0-9a-f]{32}$/),
    decision: AdjudicationDecisionSchema,
    rationale: z.string().min(10).max(4000),
    alignment: z
      .object({
        market_source_aligned: z.boolean(),
        notes: z.string().max(2000),
        resolution_mapping: z.string().max(2000).optional(),
      })
      .strict(),
    novelty: z.enum(['novel', 'already_priced', 'duplicate', 'unknown']),
    still_live: z.boolean(),
    invalidation: z.string().min(1).max(2000),
    telegram_message: z.string().min(1).max(3500),
    ledger_proposal: LedgerProposalSchema.optional(),
    paper_only: z.literal(true),
  })
  .strict();

export type Adjudication = z.infer<typeof AdjudicationSchema>;

export function parseAdjudication(value: unknown): Adjudication {
  return validate(AdjudicationSchema, value, 'Adjudication');
}
