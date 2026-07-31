import { createHash } from 'node:crypto';

import Browserbase from '@browserbasehq/sdk';
import { chromium, type Download, type Page, type Response } from 'playwright-core';

import { CollectionError, ConfigError } from '../core/errors.js';
import type { SourceSpec } from '../schema/source-spec.js';
import type { FetchedPage, SourceBrowser } from './browser.js';

export interface BrowserbaseConfig {
  apiKey: string;
  projectId: string;
  /** Reliability options read from the environment; never logged or persisted. */
  proxies: boolean;
  advancedStealth: boolean;
  region?: 'us-west-2' | 'us-east-1' | 'eu-central-1' | 'ap-southeast-1';
}

/**
 * Live deterministic collection via a Browserbase session driven by Playwright.
 *
 * Scope:
 * - one navigation to the spec's declared URL;
 * - HTML pages: optional wait_for + content();
 * - PDF / octet downloads: capture download bytes (FDA media/* style) and
 *   produce a stable text envelope for fingerprinting;
 * - no clicking, typing, form submission, login/CAPTCHA workarounds;
 * - no cookie/storage persistence.
 */
export class BrowserbaseBrowser implements SourceBrowser {
  readonly mode = 'live' as const;
  private sessionId?: string;
  private cleanup: (() => Promise<void>)[] = [];

  constructor(private readonly config: BrowserbaseConfig) {}

  async fetch(spec: SourceSpec): Promise<FetchedPage> {
    const bb = new Browserbase({ apiKey: this.config.apiKey });

    let connectUrl: string;
    try {
      const session = await bb.sessions.create({
        projectId: this.config.projectId,
        proxies: this.config.proxies,
        browserSettings: { advancedStealth: this.config.advancedStealth },
        ...(this.config.region ? { region: this.config.region } : {}),
        timeout: Math.ceil(spec.timeout_ms / 1000) + 30,
      });
      this.sessionId = session.id;
      connectUrl = session.connectUrl;
    } catch (cause) {
      throw new CollectionError('could not start a Browserbase session', {
        hint: 'Check BROWSERBASE_API_KEY / BROWSERBASE_PROJECT_ID are valid and the project has capacity. Nothing was collected.',
        cause,
      });
    }

    const browser = await chromium.connectOverCDP(connectUrl);
    this.cleanup.push(() => browser.close());

    const context = browser.contexts()[0] ?? (await browser.newContext({ acceptDownloads: true }));
    const page = context.pages()[0] ?? (await context.newPage());

    try {
      return await this.navigate(page, spec);
    } catch (cause) {
      if (cause instanceof Error && cause.name === 'TimeoutError') {
        throw new CollectionError(
          `timed out after ${spec.timeout_ms}ms waiting for ${spec.wait_for ?? 'the page'}`,
          {
            hint: 'Raise timeout_ms in the spec, or fix wait_for if the page structure changed.',
            cause,
          },
        );
      }
      throw cause;
    }
  }

  private async navigate(page: Page, spec: SourceSpec): Promise<FetchedPage> {
    const downloadWait = page
      .waitForEvent('download', { timeout: spec.timeout_ms })
      .catch(() => null);

    // eslint-disable-next-line no-useless-assignment -- set of for goto, read after catch returns
    let response: Response | null = null;
    try {
      response = await page.goto(spec.url, {
        waitUntil: 'domcontentloaded',
        timeout: spec.timeout_ms,
      });
    } catch (err) {
      const download = await downloadWait;
      if (download) {
        return this.fromDownload(download, spec);
      }
      const message = err instanceof Error ? err.message : String(err);
      if (/ERR_ABORTED|Download is starting/i.test(message)) {
        const late = await page.waitForEvent('download', { timeout: 5_000 }).catch(() => null);
        if (late) return this.fromDownload(late, spec);
      }
      throw err;
    }

    const download = await downloadWait;
    if (download) {
      return this.fromDownload(download, spec);
    }

    if (response) {
      const headers = response.headers();
      const ctype = (headers['content-type'] || '').toLowerCase();
      if (ctype.includes('application/pdf') || ctype.includes('octet-stream')) {
        const body = await response.body();
        return this.fromBinary(body, response.url(), ctype, spec);
      }
      if (response.status() >= 400) {
        throw new CollectionError(`${spec.url} returned HTTP ${response.status()}`, {
          hint: 'The desk does not retry past an error status or attempt to work around access controls. Verify the URL is publicly reachable.',
        });
      }
    }

    if (spec.wait_for) {
      await page.waitForSelector(spec.wait_for, { timeout: spec.timeout_ms });
    }
    const html = await page.content();
    return {
      html,
      final_url: page.url(),
      session_id: this.sessionId,
      content_type: 'text/html',
    };
  }

  private async fromDownload(download: Download, spec: SourceSpec): Promise<FetchedPage> {
    const failure = await download.failure();
    if (failure) {
      throw new CollectionError(`download failed for ${spec.url}: ${failure}`, {
        hint: 'Browserbase could not save the file. Check the URL still serves a public PDF.',
      });
    }
    const stream = await download.createReadStream();
    if (!stream) {
      throw new CollectionError(`download stream empty for ${spec.url}`, {
        hint: 'Retry once; if it persists the CDN may require a different accept path.',
      });
    }
    const chunks: Buffer[] = [];
    for await (const chunk of stream) {
      chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
    }
    const body = Buffer.concat(chunks);
    const suggested = download.suggestedFilename() || 'download.bin';
    const ctype = suggested.toLowerCase().endsWith('.pdf')
      ? 'application/pdf'
      : 'application/octet-stream';
    return this.fromBinary(body, download.url() || spec.url, ctype, spec);
  }

  private fromBinary(
    body: Buffer,
    finalUrl: string,
    contentType: string,
    spec: SourceSpec,
  ): FetchedPage {
    const sha = createHash('sha256').update(body).digest('hex');
    // Envelope is stable text the HTML extractor can fingerprint.
    // Specs for PDFs should use text_selector "body".
    const envelope = [
      '<!doctype html><html><body>',
      'pm-desk binary capture',
      `content-type: ${contentType}`,
      `bytes: ${body.length}`,
      `sha256: ${sha}`,
      `source_id: ${spec.id}`,
      `final_url: ${finalUrl}`,
      '</body></html>',
    ].join('\n');
    return {
      html: envelope,
      final_url: finalUrl,
      session_id: this.sessionId,
      content_type: contentType,
      binary: body,
    };
  }

  async close(): Promise<void> {
    for (const fn of this.cleanup.reverse()) {
      try {
        await fn();
      } catch {
        // Teardown failures must not mask the collection result.
      }
    }
    this.cleanup = [];
  }
}

/** Reads Browserbase settings from the environment. Values are never logged. */
export function browserbaseConfigFromEnv(
  env: Record<string, string | undefined> = process.env,
): BrowserbaseConfig {
  const apiKey = env.BROWSERBASE_API_KEY;
  const projectId = env.BROWSERBASE_PROJECT_ID;
  const missing = [
    !apiKey ? 'BROWSERBASE_API_KEY' : null,
    !projectId ? 'BROWSERBASE_PROJECT_ID' : null,
  ].filter((v): v is string => v !== null);

  if (missing.length > 0) {
    throw new ConfigError(`live collection needs ${missing.join(' and ')}`, {
      hint: 'Export the variables listed in .env.example (names only are documented; values stay out of this repo), then retry.',
    });
  }

  return {
    apiKey: apiKey!,
    projectId: projectId!,
    proxies: truthy(env.BROWSERBASE_PROXIES),
    advancedStealth: truthy(env.BROWSERBASE_ADVANCED_STEALTH),
  };
}

function truthy(value: string | undefined): boolean {
  if (!value) return false;
  return ['1', 'true', 'yes', 'on'].includes(value.toLowerCase());
}
