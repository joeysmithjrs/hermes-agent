import { CollectionError } from '../core/errors.js';
import { contentHash } from '../core/hash.js';
import { systemClock, type Clock, type IsoTimestamp } from '../core/time.js';
import type { SourceSpec } from '../schema/source-spec.js';
import type { DeskStore } from '../store/index.js';
import type { SourceBrowser } from './browser.js';
import { extractFromHtml } from './extract.js';
import { assertLandedUrlAllowed, assertNavigable } from './policy.js';

export interface CollectSourceOptions {
  browser: SourceBrowser;
  dryRun?: boolean;
  clock?: Clock;
  /** Retain the raw page HTML in the CAS. On by default for auditability. */
  retainRaw?: boolean;
}

export interface CollectSourceResult {
  source_id: string;
  spec_version: number;
  url: string;
  final_url: string;
  collected_at: IsoTimestamp;
  content_hash: string;
  previous_hash: string | null;
  changed: boolean;
  fields: Record<string, string | null>;
  normalized_artifact_ref: string | null;
  raw_artifact_ref: string | null;
  mode: 'live' | 'fixture' | 'dry-run';
  dryRun: boolean;
  text_length: number;
}

/**
 * Collect one primary source, end to end:
 *
 *   policy check → fetch → landing-url re-check → declared extraction →
 *   deterministic normalization → fingerprint → persist artifacts + provenance
 *
 * The policy check happens before the browser is touched, and the landing URL is
 * re-checked after, so neither a bad spec nor a redirect can get the desk to
 * read something it should not. The session is always closed.
 */
export async function collectSource(
  store: DeskStore,
  spec: SourceSpec,
  options: CollectSourceOptions,
): Promise<CollectSourceResult> {
  const clock = options.clock ?? systemClock;
  const dryRun = options.dryRun ?? false;

  // Before any session is opened: the cheapest place to refuse.
  assertNavigable(spec.url, spec.allowed_domains);

  let page;
  try {
    page = await options.browser.fetch(spec);
  } finally {
    await options.browser.close();
  }

  assertLandedUrlAllowed(page.final_url, spec.allowed_domains, spec.url);

  const extraction = extractFromHtml(page.html, spec);
  if (spec.fingerprint !== 'normalized_text_sha256') {
    throw new CollectionError(`unsupported fingerprint algorithm: ${spec.fingerprint}`, {
      hint: 'Only normalized_text_sha256 is implemented.',
    });
  }
  const hash = contentHash(extraction.text);
  const collected_at = clock.now();

  if (dryRun) {
    const previous = store.sources.latest(spec.id);
    return {
      source_id: spec.id,
      spec_version: spec.version,
      url: spec.url,
      final_url: page.final_url,
      collected_at,
      content_hash: hash,
      previous_hash: previous?.content_hash ?? null,
      changed: (previous?.content_hash ?? null) !== hash,
      fields: extraction.fields,
      normalized_artifact_ref: null,
      raw_artifact_ref: null,
      mode: 'dry-run',
      dryRun: true,
      text_length: extraction.text.length,
    };
  }

  const raw = options.retainRaw === false ? null : store.artifacts.put(page.html, 'text/html');

  const row = store.sources.record({
    source_id: spec.id,
    spec_version: spec.version,
    url: spec.url,
    collected_at,
    normalized_text: extraction.text,
    content_hash: hash,
    raw_artifact_ref: raw?.ref ?? null,
    fields: extraction.fields,
    collector: options.browser.mode === 'live' ? 'browserbase' : 'fixture',
    mode: options.browser.mode,
  });

  return {
    source_id: row.source_id,
    spec_version: row.spec_version,
    url: row.url,
    final_url: page.final_url,
    collected_at: row.collected_at,
    content_hash: row.content_hash,
    previous_hash: row.previous_hash,
    changed: row.changed,
    fields: row.fields,
    normalized_artifact_ref: row.normalized_artifact_ref,
    raw_artifact_ref: row.raw_artifact_ref,
    mode: options.browser.mode,
    dryRun: false,
    text_length: extraction.text.length,
  };
}
