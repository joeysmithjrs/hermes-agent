import { z } from 'zod';

import {
  ArtifactRefSchema,
  HttpsUrlSchema,
  IsoTimestampSchema,
  ProbabilitySchema,
  SeveritySchema,
  Sha256HexSchema,
  SlugSchema,
  validate,
} from './common.js';

/**
 * The SignalEnvelope is the *only* thing that crosses from deterministic code
 * into the LLM adjudication layer. It carries enough provenance (hashes,
 * artifact refs, observation timestamps) for an adjudicator to audit the claim
 * rather than trust a summary.
 */
export const SIGNAL_ENVELOPE_VERSION = 1 as const;

export const SignalKindSchema = z.enum([
  'primary_source_change',
  'market_move',
  'source_market_divergence',
]);
export type SignalKind = z.infer<typeof SignalKindSchema>;

export const MarketRefSchema = z
  .object({
    market_id: z.string().min(1),
    condition_id: z.string().min(1).optional(),
    token_id: z.string().min(1).optional(),
    outcome: z.enum(['YES', 'NO']).optional(),
    question: z.string().optional(),
    end_date: IsoTimestampSchema.optional(),
  })
  .strict();
export type MarketRef = z.infer<typeof MarketRefSchema>;

export const SourceRefSchema = z
  .object({
    source_id: SlugSchema,
    url: HttpsUrlSchema,
    previous_hash: Sha256HexSchema.nullable().optional(),
    current_hash: Sha256HexSchema,
    artifact_ref: ArtifactRefSchema,
    spec_version: z.number().int().positive().optional(),
    collected_at: IsoTimestampSchema.optional(),
  })
  .strict();
export type SourceRef = z.infer<typeof SourceRefSchema>;

export const MarketSnapshotSchema = z
  .object({
    observed_at: IsoTimestampSchema,
    mid: ProbabilitySchema.nullable().optional(),
    best_bid: ProbabilitySchema.nullable().optional(),
    best_ask: ProbabilitySchema.nullable().optional(),
    spread: z.number().min(0).max(1).nullable().optional(),
    last_trade_price: ProbabilitySchema.nullable().optional(),
    book_available: z.boolean().optional(),
  })
  .strict();
export type MarketSnapshot = z.infer<typeof MarketSnapshotSchema>;

export const SignalEvidenceSchema = z
  .object({
    diff_excerpt: z.string().max(8000).optional(),
    claims: z.array(z.string().min(1)).max(20).default([]),
    metrics: z.record(z.string(), z.number()).optional(),
  })
  .strict();

export const SignalEnvelopeSchema = z
  .object({
    version: z.literal(SIGNAL_ENVELOPE_VERSION),
    signal_id: z.string().regex(/^sig_[0-9a-f]{32}$/),
    kind: SignalKindSchema,
    severity: SeveritySchema,
    observed_at: IsoTimestampSchema,
    rule_id: SlugSchema,
    rule_version: z.string().min(1),
    market_refs: z.array(MarketRefSchema).max(20).default([]),
    source_refs: z.array(SourceRefSchema).max(20).default([]),
    market_snapshot: MarketSnapshotSchema.nullable().optional(),
    evidence: SignalEvidenceSchema,
    /**
     * Structural invariant, not a label: a payload asserting anything other than
     * `true` is rejected at every boundary in the desk.
     */
    paper_only: z.literal(true),
    dedupe_key: z.string().min(1).max(300),
  })
  .strict()
  .refine((value) => value.market_refs.length > 0 || value.source_refs.length > 0, {
    message: 'a signal must reference at least one market or one source to be auditable',
    path: ['market_refs'],
  });

export type SignalEnvelope = z.infer<typeof SignalEnvelopeSchema>;

export function parseSignalEnvelope(value: unknown): SignalEnvelope {
  return validate(SignalEnvelopeSchema, value, 'SignalEnvelope');
}

export function isSignalEnvelope(value: unknown): value is SignalEnvelope {
  return SignalEnvelopeSchema.safeParse(value).success;
}

const SEVERITY_ORDER = { info: 0, warn: 1, high: 2, critical: 3 } as const;

export function severityRank(severity: SignalEnvelope['severity']): number {
  return SEVERITY_ORDER[severity];
}
