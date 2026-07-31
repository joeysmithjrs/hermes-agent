import { z } from 'zod';

import { SchemaValidationError } from '../core/errors.js';

/**
 * Shared primitives plus the single `validate` entry point every boundary uses.
 * Nothing enters the store, the monitor, the ingress or the ledger without
 * passing through here, so a malformed payload fails at the edge with a path.
 */

/** UTC ISO-8601 with millisecond precision and a literal `Z`. */
export const IsoTimestampSchema = z
  .string()
  .regex(
    /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$/,
    'must be UTC ISO-8601 with millisecond precision, e.g. 2026-07-30T12:00:00.000Z',
  );

export const Sha256HexSchema = z.string().regex(/^[0-9a-f]{64}$/, 'must be a lowercase sha256 hex');

/** Content-addressed artifact pointer, e.g. `sha256:<64 hex>`. */
export const ArtifactRefSchema = z
  .string()
  .regex(/^sha256:[0-9a-f]{64}$/, 'must look like sha256:<64 hex chars>');

/** Prediction-market probabilities are always in [0,1]. */
export const ProbabilitySchema = z.number().min(0).max(1);

export const HttpsUrlSchema = z
  .string()
  .url()
  .refine((value) => value.startsWith('https://'), { message: 'must use https://' });

export const SeveritySchema = z.enum(['info', 'warn', 'high', 'critical']);
export type Severity = z.infer<typeof SeveritySchema>;

export const SlugSchema = z
  .string()
  .min(1)
  .max(120)
  .regex(/^[a-z0-9][a-z0-9_.-]*$/, 'must be a lowercase slug (a-z, 0-9, _, ., -)');

function formatIssues(error: z.ZodError): string {
  return error.issues
    .map((issue) => {
      const path = issue.path.length > 0 ? issue.path.join('.') : '<root>';
      return `${path}: ${issue.message}`;
    })
    .join('; ');
}

/** Parse `value` or throw a `SchemaValidationError` naming the failing paths. */
export function validate<T>(schema: z.ZodType<T>, value: unknown, label: string): T {
  const result = schema.safeParse(value);
  if (result.success) return result.data;
  const summary = formatIssues(result.error);
  throw new SchemaValidationError(`${label} failed validation — ${summary}`, {
    hint: `Fix the listed field(s) in the ${label} payload and retry.`,
    details: { issues: result.error.issues },
  });
}
