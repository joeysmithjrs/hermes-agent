import { parseHTML } from 'linkedom';

import { CollectionError } from '../core/errors.js';
import { normalizeText } from '../core/hash.js';
import type { SourceSpec } from '../schema/source-spec.js';

/**
 * Canonical form of text pulled out of HTML.
 *
 * `normalizeText` preserves line structure, which is right for plain-text
 * evidence but wrong here: HTML carries no reliable line semantics, so a page
 * that reflows its markup would otherwise produce a fingerprint change and page
 * an operator over a cosmetic edit. Every whitespace run — newlines included —
 * collapses to a single space. The stored artifact is this exact string, so
 * rehashing the artifact still reproduces `content_hash`.
 */
export function canonicalizeExtractedText(raw: string): string {
  return normalizeText(raw).replace(/\s+/g, ' ').trim();
}

export interface Extraction {
  /** Normalized text of the declared `text_selector` — the fingerprint input. */
  text: string;
  /** Declared fields only. A field that matched nothing is null, never ''. */
  fields: Record<string, string | null>;
}

/**
 * Runs the spec's declared selectors against page HTML.
 *
 * Selectors come from the spec and nowhere else — no selector is ever generated,
 * inferred or repaired at runtime. If a page changes shape the extraction fails
 * loudly and a human bumps the spec version, which keeps the fingerprint history
 * interpretable.
 *
 * The same function serves fixture and live collection, so what the offline
 * tests exercise is exactly what runs against a real page.
 */
export function extractFromHtml(html: string, spec: SourceSpec): Extraction {
  const { document } = parseHTML(html);

  const root = document.querySelector(spec.extract.text_selector);
  if (!root) {
    throw new CollectionError(
      `text_selector ${JSON.stringify(spec.extract.text_selector)} matched no element`,
      {
        hint: `The page at ${spec.url} no longer has that element. Update the selector and bump the spec version rather than loosening it at runtime.`,
        details: { source_id: spec.id, selector: spec.extract.text_selector },
      },
    );
  }

  const text = canonicalizeExtractedText(root.textContent ?? '');
  if (text.length === 0) {
    throw new CollectionError(`text_selector matched an element with no text`, {
      hint: 'An empty extraction would fingerprint as a "change" on every collection. Check the selector.',
      details: { source_id: spec.id, selector: spec.extract.text_selector },
    });
  }

  const fields: Record<string, string | null> = {};
  for (const [name, selector] of Object.entries(spec.extract.fields)) {
    const element = document.querySelector(selector);
    if (!element) {
      fields[name] = null;
      continue;
    }
    const value = canonicalizeExtractedText(element.textContent ?? '');
    fields[name] = value.length > 0 ? value : null;
  }

  return { text, fields };
}
