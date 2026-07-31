import { z } from 'zod';

import { HttpsUrlSchema, SlugSchema, validate } from './common.js';

/**
 * A SourceSpec is a fully code-owned collection recipe: the URL, the domains it
 * is allowed to touch, the selectors to read, and the fingerprint algorithm.
 * The collector never invents a selector at runtime — if a page changes shape,
 * the spec has to change and get a new version, which is itself auditable.
 */
export const FINGERPRINT_ALGORITHMS = ['normalized_text_sha256'] as const;

/** A CSS selector. Kept deliberately narrow: no XPath, no JS expressions. */
export const SelectorSchema = z
  .string()
  .min(1)
  .max(200)
  .refine((value) => !/[<>{}]|javascript:/i.test(value), {
    message: 'selector must be a plain CSS selector',
  });

export const SourceExtractSchema = z
  .object({
    text_selector: SelectorSchema,
    fields: z.record(SlugSchema, SelectorSchema).default({}),
  })
  .strict();

export const SourceSpecSchema = z
  .object({
    id: SlugSchema,
    version: z.number().int().positive().default(1),
    url: HttpsUrlSchema,
    allowed_domains: z.array(z.string().min(1).toLowerCase()).min(1).max(10),
    wait_for: SelectorSchema.optional(),
    /** Bounded so a hung page cannot hold a Browserbase session open. */
    timeout_ms: z.number().int().min(1000).max(60_000).default(20_000),
    extract: SourceExtractSchema,
    fingerprint: z.enum(FINGERPRINT_ALGORITHMS).default('normalized_text_sha256'),
    description: z.string().max(500).optional(),
    /** Optional link to the markets this source is expected to inform. */
    linked_market_ids: z.array(z.string().min(1)).max(20).default([]),
  })
  .strict();

export type SourceSpec = z.infer<typeof SourceSpecSchema>;
export type SourceExtract = z.infer<typeof SourceExtractSchema>;

export function parseSourceSpec(value: unknown): SourceSpec {
  return validate(SourceSpecSchema, value, 'SourceSpec');
}
