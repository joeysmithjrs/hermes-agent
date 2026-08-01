/**
 * Optional, best-effort live loaders. These hit public, credential-free
 * endpoints (Cleveland Fed inflation nowcasting feed; BLS public CPI series).
 * They are NOT used by any hermetic test and only run when the caller passes the
 * matching `--fetch-*` flag AND the process has opted in via `PM_DESK_LIVE_CPI=1`.
 *
 * BOIL THE OCEAN: no rung stops at the first failure. `fetchClevelandLadder`
 * tries the JSON feed the nowcasting page itself renders from, then falls back
 * to a best-effort HTML scrape of the page for the current month only.
 * `fetchBlsLadder` is the fallback for prints when the Cleveland feed's own
 * `Actual CPI Inflation` series has not been populated yet (BLS has not
 * released). Every rung — success or failure — is recorded as a
 * {@link DataPlaneAttempt}, so "the data was unavailable" is a claim with
 * evidence behind it rather than a story about one failed curl.
 */

import { UsageError } from '../../../core/errors.js';
import type { DataPlaneAttempt, NowcastRow, PrintRow } from '../types.js';
import { parseNowcastYearJson } from './cleveland_year_json.js';

const CLEVELAND_NOWCAST_YEAR_URL =
  'https://www.clevelandfed.org/-/media/files/webcharts/inflationnowcasting/nowcast_year.json?sc_lang=en';
const CLEVELAND_HTML_URL = 'https://www.clevelandfed.org/indicators-and-data/inflation-nowcasting';
const BLS_URL = 'https://api.bls.gov/publicAPI/v2/timeseries/data/CUUR0000SA0?latest=true';

/** Hard gate: live network must be explicitly opted in. */
function requireLiveOptIn(flagName: string): void {
  if (process.env.PM_DESK_LIVE_CPI !== '1') {
    throw new UsageError(
      `--${flagName} requires PM_DESK_LIVE_CPI=1 (live fetch is opt-in and never runs in hermetic tests)`,
      { hint: 'Re-run with PM_DESK_LIVE_CPI=1 set in the environment.' },
    );
  }
}

interface FetchOutcome {
  ok: boolean;
  status: number | null;
  body: string | null;
  error: string | null;
}

async function fetchText(url: string): Promise<FetchOutcome> {
  let res: Response;
  try {
    res = await fetch(url, {
      headers: { 'user-agent': 'pm-desk-cpi-nowcast/0.1 (research; paper-only)' },
      signal: AbortSignal.timeout(20_000),
    });
  } catch (err) {
    return { ok: false, status: null, body: null, error: err instanceof Error ? err.message : String(err) };
  }
  if (!res.ok) {
    return { ok: false, status: res.status, body: null, error: `HTTP ${res.status}` };
  }
  return { ok: true, status: res.status, body: await res.text(), error: null };
}

/**
 * Ladder rung 1 for both nowcasts and prints: the JSON feed the Cleveland Fed
 * nowcasting page itself renders its chart from. ~150 reference months of
 * history in one request — the intended primary source, not a fallback.
 */
async function fetchClevelandNowcastYearJson(): Promise<{
  nowcasts: NowcastRow[];
  prints: PrintRow[];
  attempt: DataPlaneAttempt;
}> {
  const source = 'cleveland_nowcast_year_json';
  const fetched = await fetchText(CLEVELAND_NOWCAST_YEAR_URL);
  if (!fetched.ok || fetched.body === null) {
    return {
      nowcasts: [],
      prints: [],
      attempt: {
        source,
        url: CLEVELAND_NOWCAST_YEAR_URL,
        ok: false,
        status: fetched.status,
        rows: 0,
        error: fetched.error ?? 'unknown fetch failure',
      },
    };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(fetched.body);
  } catch (err) {
    return {
      nowcasts: [],
      prints: [],
      attempt: {
        source,
        url: CLEVELAND_NOWCAST_YEAR_URL,
        ok: false,
        status: fetched.status,
        rows: 0,
        error: `non-JSON response (${err instanceof Error ? err.message : String(err)})`,
      },
    };
  }
  let result: { nowcasts: NowcastRow[]; prints: PrintRow[] };
  try {
    result = parseNowcastYearJson(parsed, source);
  } catch (err) {
    return {
      nowcasts: [],
      prints: [],
      attempt: {
        source,
        url: CLEVELAND_NOWCAST_YEAR_URL,
        ok: false,
        status: fetched.status,
        rows: 0,
        error: err instanceof Error ? err.message : String(err),
      },
    };
  }
  const rows = result.nowcasts.length + result.prints.length;
  return {
    nowcasts: result.nowcasts,
    prints: result.prints,
    attempt: {
      source,
      url: CLEVELAND_NOWCAST_YEAR_URL,
      ok: rows > 0,
      status: fetched.status,
      rows,
      error: rows > 0 ? null : 'parsed with zero usable rows',
    },
  };
}

/**
 * Ladder rung 2 for nowcasts only: best-effort scrape of the page itself, for
 * the current month. The page renders its chart client-side from exactly the
 * feed rung 1 reads, so this rung is a last resort that legitimately may find
 * nothing in static HTML — that is a documented, expected outcome of the
 * ladder, not a bug in the regex.
 */
async function fetchClevelandHtmlFallback(): Promise<{ nowcasts: NowcastRow[]; attempt: DataPlaneAttempt }> {
  const source = 'cleveland_html_current_month';
  const fetched = await fetchText(CLEVELAND_HTML_URL);
  if (!fetched.ok || fetched.body === null) {
    return {
      nowcasts: [],
      attempt: {
        source,
        url: CLEVELAND_HTML_URL,
        ok: false,
        status: fetched.status,
        rows: 0,
        error: fetched.error ?? 'unknown fetch failure',
      },
    };
  }
  const match = fetched.body.match(/(\d{4})[Q-](\d{2})[\s\S]{0,200}?(\d\.\d{1,2})\s*%/i);
  if (!match) {
    return {
      nowcasts: [],
      attempt: {
        source,
        url: CLEVELAND_HTML_URL,
        ok: false,
        status: fetched.status,
        rows: 0,
        error: 'could not extract a nowcast percent from static HTML (page is client-rendered from the JSON feed)',
      },
    };
  }
  const refMonth = `${match[1]}-${match[2]!.padStart(2, '0')}`;
  const nowcast = Number(match[3]);
  const nowcasts: NowcastRow[] = [{ refMonth, vintageDate: new Date(), nowcast }];
  return {
    nowcasts,
    attempt: { source, url: CLEVELAND_HTML_URL, ok: true, status: fetched.status, rows: 1, error: null },
  };
}

/**
 * The nowcast ladder. Tries the JSON feed first; only falls back to the HTML
 * scrape when the feed rung produced zero nowcast rows. Never throws for "the
 * data was not there" — every rung's outcome is recorded and returned, so a
 * caller can fail loud with evidence rather than a bare "unavailable".
 */
export async function fetchClevelandLadder(): Promise<{
  nowcasts: NowcastRow[];
  prints: PrintRow[];
  attempts: DataPlaneAttempt[];
}> {
  requireLiveOptIn('fetch-cleveland');
  const rung1 = await fetchClevelandNowcastYearJson();
  if (rung1.nowcasts.length > 0) {
    return { nowcasts: rung1.nowcasts, prints: rung1.prints, attempts: [rung1.attempt] };
  }
  const rung2 = await fetchClevelandHtmlFallback();
  return {
    nowcasts: rung2.nowcasts,
    prints: rung1.prints, // the feed may have yielded prints even with no fresh nowcast row
    attempts: [rung1.attempt, rung2.attempt],
  };
}

/**
 * BLS CPI headline YoY — fallback print source for when the Cleveland feed's
 * own `Actual CPI Inflation` series has not been populated for a reference
 * month yet (BLS has not released). Best-effort; the public BLS API is
 * rate-limited but does not require credentials.
 */
async function fetchBlsYoyRung(): Promise<{ prints: PrintRow[]; attempt: DataPlaneAttempt }> {
  const source = 'bls_public_api_cuur0000sa0';
  const fetched = await fetchText(BLS_URL);
  if (!fetched.ok || fetched.body === null) {
    return {
      prints: [],
      attempt: { source, url: BLS_URL, ok: false, status: fetched.status, rows: 0, error: fetched.error ?? 'unknown fetch failure' },
    };
  }
  let parsed: unknown;
  try {
    parsed = JSON.parse(fetched.body);
  } catch (err) {
    return {
      prints: [],
      attempt: {
        source,
        url: BLS_URL,
        ok: false,
        status: fetched.status,
        rows: 0,
        error: `non-JSON response (${err instanceof Error ? err.message : String(err)})`,
      },
    };
  }
  const series = (
    parsed as { Results?: { series?: { data?: { year: string; period: string; value: string }[] }[] } }
  )?.Results?.series?.[0]?.data;
  if (!series || series.length === 0) {
    return {
      prints: [],
      attempt: {
        source,
        url: BLS_URL,
        ok: false,
        status: fetched.status,
        rows: 0,
        error: 'BLS public API returned no CPI data (endpoint shape changed, or rate-limited)',
      },
    };
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
    const releaseDate = new Date(`${releaseYear}-${String(releaseMonth).padStart(2, '0')}-12T00:00:00.000Z`);
    out.push({ refMonth, releaseDate, yoy: Number(d.value) });
  }
  return {
    prints: out,
    attempt: { source, url: BLS_URL, ok: out.length > 0, status: fetched.status, rows: out.length, error: out.length > 0 ? null : 'parsed with zero usable rows' },
  };
}

/**
 * The prints ladder. One rung: the BLS public API. (Rung 0 — the Cleveland
 * feed's `Actual CPI Inflation` series — is read for free as part of
 * `fetchClevelandLadder`; this function is the fallback a caller reaches for
 * only when that series was empty for the reference month it needs.)
 */
export async function fetchBlsLadder(): Promise<{ prints: PrintRow[]; attempts: DataPlaneAttempt[] }> {
  requireLiveOptIn('fetch-bls');
  const rung = await fetchBlsYoyRung();
  return { prints: rung.prints, attempts: [rung.attempt] };
}
