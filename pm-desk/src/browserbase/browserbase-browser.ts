import Browserbase from '@browserbasehq/sdk';
import { chromium } from 'playwright-core';

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
 * Scope, deliberately narrow:
 * - one navigation to the spec's declared URL, one optional wait on the spec's
 *   declared `wait_for` selector, one `content()` read, then teardown;
 * - no clicking, typing, form submission, file upload or download;
 * - no cookie/storage persistence — the context is discarded with the session,
 *   and nothing is written back to disk;
 * - proxies/advanced stealth are reliability settings for sources the operator
 *   is authorized to read. There is no code here for defeating a login, paywall,
 *   CAPTCHA, geo/KYC gate, rate limit or anti-bot challenge, and adding any
 *   would be out of scope for this desk.
 */
export class BrowserbaseBrowser implements SourceBrowser {
  readonly mode = 'live';
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

    const context = browser.contexts()[0] ?? (await browser.newContext());
    const page = context.pages()[0] ?? (await context.newPage());

    try {
      const response = await page.goto(spec.url, {
        waitUntil: 'domcontentloaded',
        timeout: spec.timeout_ms,
      });
      if (response && response.status() >= 400) {
        throw new CollectionError(`${spec.url} returned HTTP ${response.status()}`, {
          hint: 'The desk does not retry past an error status or attempt to work around access controls. Verify the URL is publicly reachable.',
        });
      }
      if (spec.wait_for) {
        await page.waitForSelector(spec.wait_for, { timeout: spec.timeout_ms });
      }
      const html = await page.content();
      return { html, final_url: page.url(), session_id: this.sessionId };
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

  async close(): Promise<void> {
    // Discard the context (and with it every cookie) rather than saving state.
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
