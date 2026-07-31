import { SourcePolicyError } from '../core/errors.js';

/**
 * Navigation policy for the primary-source collector.
 *
 * The collector exists to read named public documents. It is not a general
 * browser and must never be pointed at anything transactional or authenticated.
 * These checks run *before* a session is opened, so a bad spec costs nothing and
 * reaches nothing.
 *
 * This is a refusal list, not a bypass: the desk declines these routes outright.
 * There is deliberately no code anywhere for getting past a login, paywall,
 * CAPTCHA, geo/KYC barrier, rate limit or anti-bot challenge.
 */
export const DISALLOWED_PATH_PATTERNS: readonly RegExp[] = [
  /(^|\/)wallet(s)?(\/|$)/i,
  /(^|\/)connect(-wallet)?(\/|$)/i,
  /(^|\/)login(\/|$)/i,
  /(^|\/)log-?in(\/|$)/i,
  /(^|\/)signin(\/|$)/i,
  /(^|\/)sign-?in(\/|$)/i,
  /(^|\/)sign-?up(\/|$)/i,
  /(^|\/)auth(entication)?(\/|$)/i,
  /(^|\/)oauth(\/|$)/i,
  /(^|\/)session(s)?(\/|$)/i,
  /(^|\/)account(s)?(\/|$)/i,
  /(^|\/)profile(\/|$)/i,
  /(^|\/)checkout(\/|$)/i,
  /(^|\/)cart(\/|$)/i,
  /(^|\/)payment(s)?(\/|$)/i,
  /(^|\/)billing(\/|$)/i,
  /(^|\/)trade(s|r|ing)?(\/|$)/i,
  /(^|\/)order(s)?(\/|$)/i,
  /(^|\/)position(s)?(\/|$)/i,
  /(^|\/)portfolio(\/|$)/i,
  /(^|\/)deposit(s)?(\/|$)/i,
  /(^|\/)withdraw(al)?(s)?(\/|$)/i,
  /(^|\/)transfer(s)?(\/|$)/i,
  /(^|\/)kyc(\/|$)/i,
  /(^|\/)verify(-identity)?(\/|$)/i,
  /(^|\/)admin(\/|$)/i,
];

/** Domains the desk will never drive a browser against, whatever a spec says. */
const NEVER_ALLOWED_HOSTS: readonly string[] = [
  'polymarket.com',
  'clob.polymarket.com',
  'gamma-api.polymarket.com',
];

function hostMatchesDomain(hostname: string, domain: string): boolean {
  const host = hostname.toLowerCase();
  const target = domain.toLowerCase().replace(/^\./, '');
  // Exact host, or a true subdomain. `notexample.gov` and `example.gov.evil.com`
  // must both fail, which plain `includes`/`endsWith` would not catch.
  return host === target || host.endsWith(`.${target}`);
}

/**
 * Throws unless `rawUrl` is an https URL, on a domain the spec allows, that is
 * not a sensitive route and carries no embedded credentials.
 */
export function assertNavigable(rawUrl: string, allowedDomains: readonly string[]): URL {
  let url: URL;
  try {
    url = new URL(rawUrl);
  } catch {
    throw new SourcePolicyError(`source url is not a valid URL: ${rawUrl}`, {
      hint: 'Specs must declare a fully-qualified https:// URL.',
    });
  }

  if (url.protocol !== 'https:') {
    throw new SourcePolicyError(`source url must use https, got ${url.protocol}//`, {
      hint: 'Primary-source evidence is only collected over TLS.',
      details: { url: rawUrl },
    });
  }

  if (url.username !== '' || url.password !== '') {
    throw new SourcePolicyError('source url must not embed credentials', {
      hint: 'Remove the user:password@ portion. The collector never authenticates.',
    });
  }

  if (NEVER_ALLOWED_HOSTS.some((h) => hostMatchesDomain(url.hostname, h))) {
    throw new SourcePolicyError(`the collector will not browse ${url.hostname}`, {
      hint: 'Polymarket data comes from the official public SDK, never from browser automation.',
    });
  }

  if (!allowedDomains.some((domain) => hostMatchesDomain(url.hostname, domain))) {
    throw new SourcePolicyError(
      `host ${url.hostname} is not covered by the spec's allowed_domains`,
      {
        hint: `Add it to allowed_domains only if you are authorized to collect it. Currently allowed: ${allowedDomains.join(', ')}`,
        details: { hostname: url.hostname, allowed_domains: allowedDomains },
      },
    );
  }

  const target = `${url.pathname}${url.search}`;
  const matched = DISALLOWED_PATH_PATTERNS.find((pattern) => pattern.test(target));
  if (matched) {
    throw new SourcePolicyError(`source url looks like a sensitive route: ${url.pathname}`, {
      hint: 'The collector refuses wallet, auth, account, payment and trading routes. Point the spec at a public document instead.',
      details: { pattern: matched.source },
    });
  }

  return url;
}

/** Same policy applied to the URL actually landed on, to catch redirects. */
export function assertLandedUrlAllowed(
  finalUrl: string,
  allowedDomains: readonly string[],
  declaredUrl: string,
): void {
  try {
    assertNavigable(finalUrl, allowedDomains);
  } catch (cause) {
    throw new SourcePolicyError(
      `navigation from ${declaredUrl} landed on a disallowed url: ${finalUrl}`,
      {
        hint: 'The page redirected off the allowlist. Nothing was stored. Update the spec URL if the redirect target is a legitimate public document you are authorized to read.',
        cause,
      },
    );
  }
}
