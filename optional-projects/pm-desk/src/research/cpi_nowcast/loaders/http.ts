/**
 * Optional, best-effort live loaders. These hit public, credential-free
 * endpoints (Cleveland Fed inflation nowcasting page; BLS public CPI series).
 * They are NOT used by any hermetic test and only run when the caller passes the
 * matching `--fetch-*` flag AND the process has opted in via `PM_DESK_LIVE_CPI=1`.
 *
 * These are research conveniences, not a contract: a public redesign can break
 * the extraction at any time, so failures raise a clear error instead of
 * silently emitting plausible-but-wrong rows.
 */

import { UsageError } from '../../../core/errors.js';
import type { NowcastRow, PrintRow } from '../types.js';

/** Hard gate: live network must be explicitly opted in. */
function requireLiveOptIn(flagName: string): void {
  if (process.env.PM_DESK_LIVE_CPI !== '1') {
    throw new UsageError(
      `--${flagName} requires PM_DESK_LIVE_CPI=1 (live fetch is opt-in and never runs in hermetic tests)`,
      { hint: 'Re-run with PM_DESK_LIVE_CPI=1 set in the environment.' },
    );
  }
}

async function fetchText(url: string, source: string): Promise<string> {
  let res: Response;
  try {
    res = await fetch(url, {
      headers: { 'user-agent': 'pm-desk-cpi-nowcast/0.1 (research; paper-only)' },
      signal: AbortSignal.timeout(20_000),
    });
  } catch (err) {
    throw new UsageError(`live fetch failed for ${source}`, {
      hint: 'Check connectivity; live loaders are best-effort.',
      cause: err,
    });
  }
  if (!res.ok) {
    throw new UsageError(`${source} returned HTTP ${res.status}`, {
      hint: 'The public endpoint may have moved or rate-limited the request.',
    });
  }
  return res.text();
}

/**
 * Cleveland Fed inflation nowcasting — best-effort scrape of the public page.
 * Returns the most recent nowcast rows the extractor can find; raises loudly if
 * the page shape no longer matches. Not a stable API.
 */
export async function fetchClevelandNowcasts(asOf: string): Promise<NowcastRow[]> {
  requireLiveOptIn('fetch-cleveland');
  const html = await fetchText(
    'https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting',
    'Cleveland Fed nowcasting',
  );
  // Best-effort extraction: the page embeds the latest nowcast as a percent.
  // Public pages change without notice; if we cannot find a value we fail loudly.
  const match = html.match(/(\d{4})[Q-](\d{2})[\s\S]{0,200}?(\d\.\d{1,2})\s*%/i);
  if (!match) {
    throw new UsageError('could not extract a nowcast from the Cleveland Fed page', {
      hint: 'The public page layout likely changed. Treat the live loader as broken until re-anchored.',
    });
  }
  const refMonth = `${match[1]}-${match[2]!.padStart(2, '0')}`;
  const vintageDate = new Date(`${asOf}T00:00:00.000Z`);
  const nowcast = Number(match[3]);
  return [{ refMonth, vintageDate, nowcast }];
}

/**
 * BLS CPI headline YoY — best-effort read of the public series. Returns print
 * rows for already-released months. The public BLS API is rate-limited but does
 * not require credentials.
 */
export async function fetchBlsYoy(): Promise<PrintRow[]> {
  requireLiveOptIn('fetch-bls');
  const url = 'https://api.bls.gov/publicAPI/v2/timeseries/data/CUUR0000SA0?latest=true';
  const body = await fetchText(url, 'BLS public API');
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch (err) {
    throw new UsageError('BLS public API returned non-JSON', { cause: err });
  }
  const series = (
    parsed as {
      Results?: { series?: { data?: { year: string; period: string; value: string }[] }[] };
    }
  )?.Results?.series?.[0]?.data;
  if (!series || series.length === 0) {
    throw new UsageError('BLS public API returned no CPI data', {
      hint: 'The endpoint shape may have changed or the call was rate-limited.',
    });
  }
  const out: PrintRow[] = [];
  for (const d of series) {
    const m = /^M(\d{2})$/.exec(d.period);
    if (!m) continue;
    const refMonth = `${d.year}-${m[1]}`;
    // BLS releases CPI-U for month M about two weeks into month M+1; use the 12th
    // of the following month as an approximate release date for no-look-ahead joins.
    const releaseMonth = Number(m[1]) === 12 ? 1 : Number(m[1]) + 1;
    const releaseYear = Number(m[1]) === 12 ? Number(d.year) + 1 : Number(d.year);
    const releaseDate = new Date(
      `${releaseYear}-${String(releaseMonth).padStart(2, '0')}-12T00:00:00.000Z`,
    );
    out.push({ refMonth, releaseDate, yoy: Number(d.value) });
  }
  return out;
}
