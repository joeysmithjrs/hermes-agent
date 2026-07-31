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
  /**
   * Page text submitted to the extractor. For HTML this is `page.content()`.
   * For PDF / octet downloads this is extracted text when possible, else a
   * stable binary-fingerprint envelope so content_hash still tracks the bytes.
   */
  html: string;
  /** The URL after any redirects — re-checked against the spec allowlist. */
  final_url: string;
  /** Populated in live mode for provenance; never contains credentials. */
  session_id?: string;
  /** Present when the navigation resolved to a non-HTML download. */
  content_type?: string;
  /** Raw download bytes when content_type is not HTML (kept only long enough to hash). */
  binary?: Buffer;
}

export interface SourceBrowser {
  readonly mode: 'live' | 'fixture';
  fetch(spec: SourceSpec): Promise<FetchedPage>;
  close(): Promise<void>;
}
