import { ConfigError, UsageError } from '../core/errors.js';
import type { SourceSpec } from '../schema/source-spec.js';
import type { SourceBrowser } from './browser.js';
import { FixtureBrowser } from './fixture-browser.js';
import { assertNavigable } from './policy.js';

/** The flag an operator must type to authorize a real Browserbase session. */
export const LIVE_CONFIRM_FLAG = 'i-understand-this-uses-a-live-browserbase-session';

export interface ResolveBrowserOptions {
  live: boolean;
  confirmed: boolean;
  spec: SourceSpec;
  fixture?: string;
  env?: Record<string, string | undefined>;
}

/**
 * Chooses the collection backend. Live Browserbase is opt-in twice over: an
 * explicit `--live` and a separate confirmation flag. Anything less falls back
 * to a fixture, and a missing fixture is an error rather than an invented page.
 */
export async function resolveBrowser(options: ResolveBrowserOptions): Promise<SourceBrowser> {
  if (!options.live) {
    if (!options.fixture) {
      throw new UsageError('offline collection needs --fixture <path-to-html>', {
        hint: `Point --fixture at a saved copy of the page, or run live with --live --${LIVE_CONFIRM_FLAG}.`,
      });
    }
    return new FixtureBrowser(options.fixture);
  }

  if (!options.confirmed) {
    throw new UsageError(
      `live Browserbase collection requires the explicit confirmation flag --${LIVE_CONFIRM_FLAG}`,
      {
        hint: `A live run opens a billed remote browser session against ${options.spec.url}. Re-run with --live --${LIVE_CONFIRM_FLAG} if that is what you want.`,
      },
    );
  }

  // Refuse before we even look at credentials.
  assertNavigable(options.spec.url, options.spec.allowed_domains);

  const { BrowserbaseBrowser, browserbaseConfigFromEnv } = await import('./browserbase-browser.js');
  const config = browserbaseConfigFromEnv(options.env ?? process.env);
  if (!config.apiKey || !config.projectId) {
    throw new ConfigError('BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID must be set', {
      hint: 'See .env.example for the variable names.',
    });
  }
  return new BrowserbaseBrowser(config);
}
