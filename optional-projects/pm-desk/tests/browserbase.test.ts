import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { contentHash } from '../src/core/hash.js';
import { CollectionError, SourcePolicyError } from '../src/core/errors.js';
import { fixedClock } from '../src/core/time.js';
import { collectSource } from '../src/browserbase/collector.js';
import { extractFromHtml } from '../src/browserbase/extract.js';
import { FixtureBrowser } from '../src/browserbase/fixture-browser.js';
import { DISALLOWED_PATH_PATTERNS, assertNavigable } from '../src/browserbase/policy.js';
import { loadSourceSpec } from '../src/browserbase/spec-loader.js';
import { openStore, type DeskStore } from '../src/store/index.js';

const SPEC = {
  id: 'example_official_release',
  version: 1,
  url: 'https://example.gov/release',
  allowed_domains: ['example.gov'],
  wait_for: 'main',
  timeout_ms: 20_000,
  extract: {
    text_selector: 'main',
    fields: { title: 'h1', published_at: 'time' },
  },
  fingerprint: 'normalized_text_sha256' as const,
  linked_market_ids: [],
};

const HTML_V1 = `<!doctype html><html><body>
  <nav>Nav junk that must not be fingerprinted</nav>
  <main>
    <h1>Q1 Advance Estimate</h1>
    <time datetime="2026-07-30">July 30, 2026</time>
    <p>Real GDP increased at an annual rate of 3.1 percent.</p>
  </main>
  <footer>Copyright</footer>
</body></html>`;

const HTML_V2 = HTML_V1.replace('3.1 percent', '2.4 percent');

let dir: string;
let store: DeskStore;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'pm-desk-bb-'));
  store = openStore({ home: dir });
});

afterEach(() => {
  store.close();
  rmSync(dir, { recursive: true, force: true });
});

describe('navigation policy', () => {
  it('accepts an https url on an allowlisted domain', () => {
    expect(() => assertNavigable('https://example.gov/release', ['example.gov'])).not.toThrow();
  });

  it('accepts a subdomain of an allowlisted domain but not a lookalike suffix', () => {
    expect(() => assertNavigable('https://data.example.gov/x', ['example.gov'])).not.toThrow();
    expect(() => assertNavigable('https://notexample.gov/x', ['example.gov'])).toThrow(
      SourcePolicyError,
    );
    expect(() => assertNavigable('https://example.gov.evil.com/x', ['example.gov'])).toThrow(
      SourcePolicyError,
    );
  });

  it('rejects plaintext http', () => {
    expect(() => assertNavigable('http://example.gov/release', ['example.gov'])).toThrow(
      SourcePolicyError,
    );
  });

  it('rejects a domain that is not on the spec allowlist', () => {
    expect(() => assertNavigable('https://elsewhere.com/x', ['example.gov'])).toThrow(
      SourcePolicyError,
    );
  });

  it('rejects credentials embedded in the url', () => {
    expect(() => assertNavigable('https://user:pw@example.gov/x', ['example.gov'])).toThrow(
      SourcePolicyError,
    );
  });

  it.each([
    'https://example.gov/wallet/connect',
    'https://example.gov/login',
    'https://example.gov/sign-in',
    'https://example.gov/checkout',
    'https://example.gov/trade/BTC',
    'https://example.gov/orders/new',
    'https://example.gov/account/settings',
    'https://example.gov/kyc',
    'https://example.gov/deposit',
  ])('refuses the sensitive route %s', (url) => {
    expect(() => assertNavigable(url, ['example.gov'])).toThrow(SourcePolicyError);
  });

  it('declares patterns covering every sensitive route family the desk must refuse', () => {
    for (const needle of ['wallet', 'login', 'signin', 'checkout', 'trade', 'order', 'kyc']) {
      expect(DISALLOWED_PATH_PATTERNS.some((p) => p.source.includes(needle))).toBe(true);
    }
  });
});

describe('deterministic extraction', () => {
  it('extracts only the declared selectors and fields', () => {
    const result = extractFromHtml(HTML_V1, SPEC);
    expect(result.text).toContain('Real GDP increased');
    expect(result.text).not.toContain('Nav junk');
    expect(result.text).not.toContain('Copyright');
    expect(result.fields).toEqual({
      title: 'Q1 Advance Estimate',
      published_at: 'July 30, 2026',
    });
  });

  it('is stable across cosmetic markup churn but changes on real content change', () => {
    const reflowed = HTML_V1.replace(/\n\s*/g, ' ').replace('<main>', '<main >');
    expect(contentHash(extractFromHtml(reflowed, SPEC).text)).toBe(
      contentHash(extractFromHtml(HTML_V1, SPEC).text),
    );
    expect(contentHash(extractFromHtml(HTML_V2, SPEC).text)).not.toBe(
      contentHash(extractFromHtml(HTML_V1, SPEC).text),
    );
  });

  it('records a missing field as null rather than an empty-string false negative', () => {
    const html = '<html><body><main><h1>Only a title</h1></main></body></html>';
    const result = extractFromHtml(html, SPEC);
    expect(result.fields.published_at).toBeNull();
    expect(result.fields.title).toBe('Only a title');
  });

  it('fails loudly when the required text selector matches nothing', () => {
    const html = '<html><body><article>no main element</article></body></html>';
    expect(() => extractFromHtml(html, SPEC)).toThrow(CollectionError);
  });
});

describe('spec loading', () => {
  it('loads and validates a YAML spec from disk', () => {
    const path = join(dir, 'spec.yaml');
    writeFileSync(
      path,
      [
        'id: example_official_release',
        'url: https://example.gov/release',
        'allowed_domains: [example.gov]',
        'wait_for: "main"',
        'extract:',
        '  text_selector: "main"',
        '  fields:',
        '    title: "h1"',
        'fingerprint: normalized_text_sha256',
      ].join('\n'),
    );
    const spec = loadSourceSpec(path);
    expect(spec.id).toBe('example_official_release');
    expect(spec.version).toBe(1);
  });

  it('reports an unreadable spec path with an actionable error', () => {
    expect(() => loadSourceSpec(join(dir, 'missing.yaml'))).toThrow(/missing.yaml/);
  });
});

describe('collectSource', () => {
  const fixturePath = () => {
    const p = join(dir, 'fixture-v1.html');
    writeFileSync(p, HTML_V1);
    return p;
  };

  it('dry run validates and extracts without writing to the store', async () => {
    const result = await collectSource(store, SPEC, {
      browser: new FixtureBrowser(fixturePath()),
      dryRun: true,
      clock: fixedClock('2026-07-30T12:00:00.000Z'),
    });

    expect(result.dryRun).toBe(true);
    expect(result.content_hash).toBe(contentHash(extractFromHtml(HTML_V1, SPEC).text));
    expect(store.sources.latest(SPEC.id)).toBeUndefined();
  });

  it('persists the snapshot, its provenance and both artifacts', async () => {
    const result = await collectSource(store, SPEC, {
      browser: new FixtureBrowser(fixturePath()),
      clock: fixedClock('2026-07-30T12:00:00.000Z'),
    });

    expect(result.changed).toBe(true);
    const stored = store.sources.latest(SPEC.id);
    expect(stored?.url).toBe(SPEC.url);
    expect(stored?.collected_at).toBe('2026-07-30T12:00:00.000Z');
    expect(stored?.spec_version).toBe(1);
    expect(store.artifacts.read(stored!.normalized_artifact_ref)).toContain('3.1 percent');
    expect(store.artifacts.verify(stored!.raw_artifact_ref!)).toBe(true);
    expect(stored?.fields.title).toBe('Q1 Advance Estimate');
  });

  it('reports no change on re-collection of identical content', async () => {
    const opts = {
      browser: new FixtureBrowser(fixturePath()),
      clock: fixedClock('2026-07-30T12:00:00.000Z'),
    };
    await collectSource(store, SPEC, opts);
    const second = await collectSource(store, SPEC, {
      ...opts,
      clock: fixedClock('2026-07-30T12:05:00.000Z'),
    });
    expect(second.changed).toBe(false);
  });

  it('detects a real change and links the previous hash', async () => {
    const v1 = join(dir, 'v1.html');
    const v2 = join(dir, 'v2.html');
    writeFileSync(v1, HTML_V1);
    writeFileSync(v2, HTML_V2);

    const first = await collectSource(store, SPEC, {
      browser: new FixtureBrowser(v1),
      clock: fixedClock('2026-07-30T12:00:00.000Z'),
    });
    const second = await collectSource(store, SPEC, {
      browser: new FixtureBrowser(v2),
      clock: fixedClock('2026-07-30T12:05:00.000Z'),
    });

    expect(second.changed).toBe(true);
    expect(second.previous_hash).toBe(first.content_hash);
  });

  it('refuses to start a session when the spec url violates policy', async () => {
    const badSpec = { ...SPEC, url: 'https://example.gov/wallet/connect' };
    const browser = new FixtureBrowser(fixturePath());
    await expect(
      collectSource(store, badSpec, { browser, clock: fixedClock('2026-07-30T12:00:00.000Z') }),
    ).rejects.toThrow(SourcePolicyError);
    // The policy check runs before any navigation.
    expect(browser.navigations).toBe(0);
  });

  it('refuses when the spec url is not covered by its own allowlist', async () => {
    const badSpec = { ...SPEC, url: 'https://other.example.com/release' };
    const browser = new FixtureBrowser(fixturePath());
    await expect(
      collectSource(store, badSpec, { browser, clock: fixedClock('2026-07-30T12:00:00.000Z') }),
    ).rejects.toThrow(SourcePolicyError);
    expect(browser.navigations).toBe(0);
  });

  it('refuses a redirect that lands off the allowlist, and stores nothing', async () => {
    const browser = new FixtureBrowser(fixturePath(), {
      finalUrl: 'https://tracker.evil.com/redirected',
    });
    await expect(
      collectSource(store, SPEC, { browser, clock: fixedClock('2026-07-30T12:00:00.000Z') }),
    ).rejects.toThrow(SourcePolicyError);
    expect(store.sources.latest(SPEC.id)).toBeUndefined();
  });

  it('always closes the browser session, including on failure', async () => {
    const browser = new FixtureBrowser(fixturePath(), { failNavigation: true });
    await expect(
      collectSource(store, SPEC, { browser, clock: fixedClock('2026-07-30T12:00:00.000Z') }),
    ).rejects.toThrow(CollectionError);
    expect(browser.closed).toBe(true);
  });
});

describe('live mode guard', () => {
  it('refuses live collection without the explicit confirmation flag', async () => {
    const { resolveBrowser } = await import('../src/browserbase/browser-factory.js');
    await expect(resolveBrowser({ live: true, confirmed: false, spec: SPEC })).rejects.toThrow(
      /--i-understand-this-uses-a-live-browserbase-session/,
    );
  });

  it('refuses live collection when Browserbase credentials are absent', async () => {
    const { resolveBrowser } = await import('../src/browserbase/browser-factory.js');
    const env = { BROWSERBASE_API_KEY: '', BROWSERBASE_PROJECT_ID: '' };
    await expect(resolveBrowser({ live: true, confirmed: true, spec: SPEC, env })).rejects.toThrow(
      /BROWSERBASE_API_KEY/,
    );
  });

  it('defaults to the fixture browser when live mode is not requested', async () => {
    const { resolveBrowser } = await import('../src/browserbase/browser-factory.js');
    const p = join(dir, 'f.html');
    writeFileSync(p, HTML_V1);
    const browser = await resolveBrowser({ live: false, confirmed: false, spec: SPEC, fixture: p });
    expect(browser.mode).toBe('fixture');
  });

  it('requires a fixture path when not live, rather than silently inventing content', async () => {
    const { resolveBrowser } = await import('../src/browserbase/browser-factory.js');
    await expect(resolveBrowser({ live: false, confirmed: false, spec: SPEC })).rejects.toThrow(
      /--fixture/,
    );
  });
});
