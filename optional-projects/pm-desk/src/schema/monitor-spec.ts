import { z } from 'zod';

import { SeveritySchema, SlugSchema, validate } from './common.js';

/**
 * Monitor specs are declarative data, never code and never a prompt. The engine
 * reads a spec plus stored observations and decides. A discriminated union keeps
 * each rule's params honest: a `primary_source_change` spec cannot carry
 * `abs_move`, so a copy-paste mistake fails validation instead of silently
 * evaluating a rule nobody configured.
 */

const MarketBindingSchema = z
  .object({
    token_id: z.string().min(1),
    market_id: z.string().min(1).optional(),
    condition_id: z.string().min(1).optional(),
    outcome: z.enum(['YES', 'NO']).optional(),
  })
  .strict();

const PrimarySourceChangeParams = z
  .object({
    source_id: SlugSchema,
    /** Optional markets to attach to the emitted envelope for context. */
    market_refs: z.array(MarketBindingSchema).max(10).default([]),
    /** Ignore snapshots older than this; prevents alerting on stale evidence. */
    max_staleness_s: z
      .number()
      .int()
      .positive()
      .max(86_400 * 30)
      .default(86_400),
    min_diff_chars: z.number().int().min(0).default(1),
  })
  .strict();

const MarketMoveParams = z
  .object({
    token_id: z.string().min(1),
    market_id: z.string().min(1).optional(),
    condition_id: z.string().min(1).optional(),
    outcome: z.enum(['YES', 'NO']).optional(),
    /** Absolute probability move, e.g. 0.05 = five points. */
    abs_move: z.number().gt(0).max(1).optional(),
    /** Relative move vs the reference mid, e.g. 0.25 = 25%. */
    rel_move: z.number().gt(0).max(10).optional(),
    lookback_s: z
      .number()
      .int()
      .positive()
      .max(86_400 * 7)
      .default(3600),
    max_staleness_s: z.number().int().positive().max(86_400).default(900),
  })
  .strict()
  .refine((value) => value.abs_move !== undefined || value.rel_move !== undefined, {
    message: 'market_move requires at least one of abs_move or rel_move',
    path: ['abs_move'],
  });

const SpreadWideningParams = z
  .object({
    token_id: z.string().min(1),
    market_id: z.string().min(1).optional(),
    outcome: z.enum(['YES', 'NO']).optional(),
    /** Emit when the observed spread meets or exceeds this width. */
    max_spread: z.number().gt(0).max(1).default(0.05),
    /** Emit when the book is missing entirely (data-quality alarm). */
    alert_on_missing_book: z.boolean().default(true),
    max_staleness_s: z.number().int().positive().max(86_400).default(900),
  })
  .strict();

const SourceMarketDivergenceParams = z
  .object({
    source_id: SlugSchema,
    market: MarketBindingSchema,
    /** Only fire while the market ends within this horizon from `now`. */
    horizon_s: z
      .number()
      .int()
      .positive()
      .max(86_400 * 365)
      .default(86_400 * 7),
    require_active: z.boolean().default(true),
    max_staleness_s: z.number().int().positive().max(86_400).default(3600),
  })
  .strict();

const BaseFields = {
  id: SlugSchema,
  version: z.number().int().positive().default(1),
  enabled: z.boolean().default(true),
  severity: SeveritySchema.default('warn'),
  /** Minimum seconds between two emissions for the same dedupe key. */
  cooldown_s: z
    .number()
    .int()
    .min(0)
    .max(86_400 * 30)
    .default(3600),
  description: z.string().max(500).optional(),
};

export const MonitorSpecSchema = z.discriminatedUnion('kind', [
  z
    .object({
      ...BaseFields,
      kind: z.literal('primary_source_change'),
      params: PrimarySourceChangeParams,
    })
    .strict(),
  z.object({ ...BaseFields, kind: z.literal('market_move'), params: MarketMoveParams }).strict(),
  z
    .object({ ...BaseFields, kind: z.literal('spread_widening'), params: SpreadWideningParams })
    .strict(),
  z
    .object({
      ...BaseFields,
      kind: z.literal('source_market_divergence'),
      params: SourceMarketDivergenceParams,
    })
    .strict(),
]);

export type MonitorSpec = z.infer<typeof MonitorSpecSchema>;
export type MonitorKind = MonitorSpec['kind'];

export function parseMonitorSpec(value: unknown): MonitorSpec {
  return validate(MonitorSpecSchema, value, 'MonitorSpec');
}
