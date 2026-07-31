import { createHash } from 'node:crypto';

/**
 * Content fingerprinting for primary-source snapshots.
 *
 * A source page changes its markup constantly (whitespace, reflow, invisible
 * tracking characters) without changing what it *says*. `normalizeText` strips
 * exactly those cosmetic differences and nothing else, so a fingerprint change
 * is evidence of a real content change rather than a rendering artifact.
 */

export function sha256Hex(input: string | Uint8Array): string {
  return createHash('sha256').update(input).digest('hex');
}

/**
 * Zero-width / BOM / soft-hyphen characters that carry no visible content.
 * Written as an alternation of explicit escapes rather than a character class:
 * ZWNJ+ZWJ inside a class reads as a joined sequence, and literal invisible
 * characters in source are impossible to review.
 */
const INVISIBLE = new RegExp(
  ['\\u00AD', '\\u200B', '\\u200C', '\\u200D', '\\u2060', '\\uFEFF'].join('|'),
  'gu',
);

export function normalizeText(input: string): string {
  return input
    .normalize('NFC')
    .replace(INVISIBLE, '')
    .replace(/\r\n?/g, '\n')
    .replace(/[^\S\n]+/g, ' ') // collapse horizontal whitespace, keep newlines
    .split('\n')
    .map((line) => line.trim())
    .join('\n')
    .replace(/\n{3,}/g, '\n\n') // collapse blank-line runs
    .trim();
}

/** `normalized_text_sha256`: the desk's default source fingerprint algorithm. */
export function contentHash(input: string): string {
  return sha256Hex(normalizeText(input));
}

/** Deterministic JSON with sorted object keys, for hashing structured values. */
export function stableStringify(value: unknown): string {
  return JSON.stringify(sortValue(value));
}

function sortValue(value: unknown): unknown {
  if (Array.isArray(value)) return value.map(sortValue);
  if (value && typeof value === 'object' && !(value instanceof Date)) {
    const source = value as Record<string, unknown>;
    const out: Record<string, unknown> = {};
    for (const key of Object.keys(source).sort()) out[key] = sortValue(source[key]);
    return out;
  }
  return value;
}

export function objectHash(value: unknown): string {
  return sha256Hex(stableStringify(value));
}
