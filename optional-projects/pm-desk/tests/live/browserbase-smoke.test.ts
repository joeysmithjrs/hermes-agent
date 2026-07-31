import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterAll, beforeAll, describe, expect, it } from 'vitest';

import { resolveBrowser } from '../../src/browserbase/browser-factory.js';
import { collectSource } from '../../src/browserbase/collector.js';
import { parseSourceSpec } from '../../src/schema/source-spec.js';
import { openStore, type DeskStore } from '../../src/store/index.js';

/**
 * OPT-IN live Browserbase check. Requires BOTH:
 *
 *   PM_DESK_LIVE=1  PM_DESK_LIVE_BROWSERBASE=1  npm run test:live
 *
 * plus BROWSERBASE_API_KEY and BROWSERBASE_PROJECT_ID in the environment. It
 * skips silently otherwise, so it can never bill a session by accident.
 *
 * The target is example.com — a domain reserved by IANA precisely for this kind
 * of use, with no login, no paywall and no anti-bot barrier.
 */
const ENABLED =
  process.env.PM_DESK_LIVE === '1' &&
  process.env.PM_DESK_LIVE_BROWSERBASE === '1' &&
  Boolean(process.env.BROWSERBASE_API_KEY) &&
  Boolean(process.env.BROWSERBASE_PROJECT_ID);

const suite = ENABLED ? describe : describe.skip;

const SPEC = parseSourceSpec({
  id: 'iana_example_page',
  version: 1,
  url: 'https://example.com/',
  allowed_domains: ['example.com'],
  wait_for: 'body',
  timeout_ms: 30_000,
  extract: { text_selector: 'body', fields: { title: 'h1' } },
  fingerprint: 'normalized_text_sha256',
});

let dir: string;
let store: DeskStore;

beforeAll(() => {
  if (!ENABLED) return;
  dir = mkdtempSync(join(tmpdir(), 'pm-desk-bblive-'));
  store = openStore({ home: dir });
});

afterAll(() => {
  if (!ENABLED) return;
  store.close();
  rmSync(dir, { recursive: true, force: true });
});

suite('browserbase live collection', () => {
  it('collects a public page through a real Browserbase session', async () => {
    const browser = await resolveBrowser({ live: true, confirmed: true, spec: SPEC });
    expect(browser.mode).toBe('live');

    const result = await collectSource(store, SPEC, { browser });

    expect(result.mode).toBe('live');
    expect(result.content_hash).toMatch(/^[0-9a-f]{64}$/);
    expect(result.text_length).toBeGreaterThan(20);
    expect(result.fields.title).toBeTruthy();

    // The stored artifact must rehash to the recorded fingerprint.
    const stored = store.sources.latest(SPEC.id)!;
    expect(stored.normalized_artifact_ref).toBe(`sha256:${result.content_hash}`);
    expect(store.artifacts.verify(stored.normalized_artifact_ref)).toBe(true);
  }, 120_000);
});
