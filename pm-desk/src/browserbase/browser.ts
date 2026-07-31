import type { SourceSpec } from '../schema/source-spec.js';

/**
 * The seam between "get the bytes" and "interpret the bytes".
 *
 * A SourceBrowser only fetches HTML and reports where it actually landed.
 * Extraction, normalization, hashing and persistence all live in deterministic
 * code on the other side of this interface, so the entire collector is testable
 * with a fake and live Browserbase is one swappable implementation.
 */
export interface FetchedPage {
  html: string;
  /** The URL after any redirects — re-checked against the spec allowlist. */
  final_url: string;
  /** Populated in live mode for provenance; never contains credentials. */
  session_id?: string;
}

export interface SourceBrowser {
  readonly mode: 'live' | 'fixture';
  fetch(spec: SourceSpec): Promise<FetchedPage>;
  close(): Promise<void>;
}
