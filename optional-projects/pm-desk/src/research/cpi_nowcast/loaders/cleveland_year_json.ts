/**
 * Parser for the Cleveland Fed's `nowcast_year.json` chart feed.
 *
 * This is the primary rung of the live nowcast ladder (see `http.ts`), because
 * the HTML page it used to be scraped from does not embed the nowcast value in
 * static markup at all — the chart is client-rendered from exactly this JSON.
 * A regex against the page is a fossil of an old page shape; this file is the
 * feed the page itself reads.
 *
 * Shape (FusionCharts multi-series `msline`, one entry per CPI reference
 * month, oldest first): each entry's `chart.subcaption` is the reference month
 * (`"YYYY-M"`), `categories[0].category` is the shared date axis for every
 * series in that entry, and `dataset[]` holds one series per line — the ones
 * this parser reads are `"CPI Inflation"` (the nowcast) and `"Actual CPI
 * Inflation"` (the released print, blank until BLS publishes it). A handful of
 * axis entries are vertical-line markers (`"CPI Mar"`, `"PCE Feb"`) rather than
 * dates and carry no data; they are skipped, not parsed.
 *
 * Axis labels are `MM/DD` with no year, because a chart entry's own window
 * (roughly the reference month plus the following month, until that month's
 * print appears) can cross a December→January boundary. Year is tracked by
 * rollover: it starts at the reference month's own year and increments every
 * time the parsed month number goes backwards relative to the last one seen —
 * safe because the axis is otherwise monotonically non-decreasing.
 */

import { UsageError } from '../../../core/errors.js';
import type { NowcastRow, PrintRow } from '../types.js';

const CPI_SERIES_NAME = 'CPI Inflation';
const ACTUAL_SERIES_NAME = 'Actual CPI Inflation';

interface ParsedNowcastYear {
  nowcasts: NowcastRow[];
  prints: PrintRow[];
}

/** `"2026-4"` / `"2026-04"` -> `"2026-04"`. Throws on anything else. */
function parseSubcaption(subcaption: unknown, source: string): { year: number; month: number; refMonth: string } {
  const m = typeof subcaption === 'string' ? /^(\d{4})-(\d{1,2})$/.exec(subcaption.trim()) : null;
  if (!m) {
    throw new UsageError(`unrecognized chart subcaption in ${source}: ${JSON.stringify(subcaption)}`, {
      hint: 'Expected "YYYY-M", e.g. "2026-4". The feed shape may have changed.',
    });
  }
  const year = Number(m[1]);
  const month = Number(m[2]);
  return { year, month, refMonth: `${m[1]}-${month.toString().padStart(2, '0')}` };
}

function numericValue(cell: unknown): number | null {
  if (cell === null || typeof cell !== 'object') return null;
  const value = (cell as { value?: unknown }).value;
  if (typeof value !== 'string' || value.trim() === '') return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

/**
 * Parse one `nowcast_year.json` payload into every no-look-ahead-eligible
 * nowcast/print row it contains. Malformed chart entries (missing the two
 * series this parser reads) are skipped rather than failing the whole feed —
 * a single reference month's chart shifting shape should not blank out 150
 * others that still parse cleanly. The whole payload failing to parse at all
 * (not an array, zero usable rows) is the caller's concern via the attempt log.
 */
export function parseNowcastYearJson(raw: unknown, source: string): ParsedNowcastYear {
  if (!Array.isArray(raw)) {
    throw new UsageError(`${source} is not a JSON array`, {
      hint: 'Expected the FusionCharts multi-series array the Cleveland Fed nowcasting page renders from.',
    });
  }

  const nowcasts: NowcastRow[] = [];
  const prints: PrintRow[] = [];

  for (const entry of raw) {
    if (entry === null || typeof entry !== 'object') continue;
    const chart = (entry as { chart?: unknown }).chart;
    const subcaption = chart !== null && typeof chart === 'object' ? (chart as { subcaption?: unknown }).subcaption : undefined;
    if (subcaption === undefined) continue;

    let parsedMonth: ReturnType<typeof parseSubcaption>;
    try {
      parsedMonth = parseSubcaption(subcaption, source);
    } catch {
      continue; // one unreadable month label does not sink the whole feed
    }
    const { refMonth } = parsedMonth;

    const categoryBlock = (entry as { categories?: unknown }).categories;
    const category = Array.isArray(categoryBlock) ? (categoryBlock[0] as { category?: unknown } | undefined)?.category : undefined;
    const dataset = (entry as { dataset?: unknown }).dataset;
    if (!Array.isArray(category) || !Array.isArray(dataset)) continue;

    const cpiSeries = dataset.find(
      (d): d is { seriesname: string; data: unknown[] } =>
        d !== null && typeof d === 'object' && (d as { seriesname?: unknown }).seriesname === CPI_SERIES_NAME,
    );
    const actualSeries = dataset.find(
      (d): d is { seriesname: string; data: unknown[] } =>
        d !== null && typeof d === 'object' && (d as { seriesname?: unknown }).seriesname === ACTUAL_SERIES_NAME,
    );
    if (!cpiSeries || !Array.isArray(cpiSeries.data) || !actualSeries || !Array.isArray(actualSeries.data)) continue;

    let year = parsedMonth.year;
    let lastMonth = parsedMonth.month;
    let printedForThisMonth = false;
    const n = Math.min(category.length, cpiSeries.data.length, actualSeries.data.length);

    for (let i = 0; i < n; i += 1) {
      const cat = category[i];
      const label = cat !== null && typeof cat === 'object' ? (cat as { label?: unknown }).label : undefined;
      const dateMatch = typeof label === 'string' ? /^(\d{2})\/(\d{2})$/.exec(label) : null;
      if (!dateMatch) continue; // a vline marker ("CPI Mar"), not a dated axis point

      const month = Number(dateMatch[1]);
      const day = dateMatch[2];
      if (month < lastMonth) year += 1;
      lastMonth = month;
      const iso = `${year}-${dateMatch[1]}-${day}`;
      const date = new Date(`${iso}T00:00:00.000Z`);

      const nowcastValue = numericValue(cpiSeries.data[i]);
      if (nowcastValue !== null) {
        nowcasts.push({ refMonth, vintageDate: date, nowcast: nowcastValue });
      }

      // The chart plots the actual print as a single marker point on the day
      // it first appears, not a repeated flat line — take the first one.
      if (!printedForThisMonth) {
        const actualValue = numericValue(actualSeries.data[i]);
        if (actualValue !== null) {
          prints.push({ refMonth, releaseDate: date, yoy: actualValue });
          printedForThisMonth = true;
        }
      }
    }
  }

  return { nowcasts, prints };
}
