import { existsSync, readFileSync } from 'node:fs';

import { CollectionError } from '../core/errors.js';
import type { SourceSpec } from '../schema/source-spec.js';
import type { FetchedPage, SourceBrowser } from './browser.js';

export interface FixtureBrowserOptions {
  /** Override the reported landing URL, e.g. to exercise redirect policy. */
  finalUrl?: string;
  failNavigation?: boolean;
}

/**
 * Reads a local HTML file instead of driving a browser. This is the default
 * collection mode: it makes the whole pipeline — including the offline E2E —
 * runnable with no network, no credentials and no Browserbase session.
 */
export class FixtureBrowser implements SourceBrowser {
  readonly mode = 'fixture';
  navigations = 0;
  closed = false;

  constructor(
    private readonly path: string,
    private readonly options: FixtureBrowserOptions = {},
  ) {}

  async fetch(spec: SourceSpec): Promise<FetchedPage> {
    this.navigations += 1;
    if (this.options.failNavigation) {
      throw new CollectionError(`fixture navigation failed for ${spec.id}`, {
        hint: 'This fixture is configured to simulate a navigation failure.',
      });
    }
    if (!existsSync(this.path)) {
      throw new CollectionError(`fixture not found: ${this.path}`, {
        hint: 'Pass --fixture <path-to-html>, or use --live with explicit confirmation.',
      });
    }
    return {
      html: readFileSync(this.path, 'utf8'),
      final_url: this.options.finalUrl ?? spec.url,
    };
  }

  async close(): Promise<void> {
    this.closed = true;
  }
}
