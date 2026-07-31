/**
 * Loaders for CPI nowcast calibration.
 *
 * The hermetic path reads CSV/JSON fixtures from disk and never touches the
 * network. The live path (Cleveland Fed nowcasting + BLS CPI YoY) is opt-in and
 * requires no credentials — it is best-effort and gated behind `PM_DESK_LIVE_CPI=1`.
 */

import { readFileSync } from 'node:fs';

import { UsageError } from '../../../core/errors.js';
import type { NowcastRow, PrintRow } from '../types.js';

export { fetchClevelandNowcasts, fetchBlsYoy } from './http.js';

/** Parse a `YYYY-MM` reference month and validate it. */
function parseRefMonth(value: string, source: string): string {
  const m = /^(\d{4})-(\d{2})$/.exec(value.trim());
  if (!m) {
    throw new UsageError(`bad ref_month ${JSON.stringify(value)} in ${source}`, {
      hint: 'Use YYYY-MM, e.g. 2025-06.',
    });
  }
  const month = Number(m[2]);
  if (month < 1 || month > 12) {
    throw new UsageError(`bad ref_month ${JSON.stringify(value)} in ${source}`, {
      hint: 'Month must be 01–12.',
    });
  }
  return value.trim();
}

/** Parse a `YYYY-MM-DD` (or full ISO) date as a UTC midnight Date. */
function parseDate(value: string, field: string, source: string): Date {
  const trimmed = value.trim();
  const dateOnly = /^(\d{4})-(\d{2})-(\d{2})$/.exec(trimmed);
  if (dateOnly) {
    const d = new Date(`${trimmed}T00:00:00.000Z`);
    if (Number.isNaN(d.getTime())) {
      throw new UsageError(`bad ${field} ${JSON.stringify(value)} in ${source}`, {
        hint: 'Use YYYY-MM-DD.',
      });
    }
    return d;
  }
  const d = new Date(trimmed);
  if (Number.isNaN(d.getTime())) {
    throw new UsageError(`bad ${field} ${JSON.stringify(value)} in ${source}`, {
      hint: 'Use an ISO date, e.g. 2025-06-12 or 2025-06-12T12:00:00Z.',
    });
  }
  return d;
}

function parseFloatField(value: string, field: string, source: string): number {
  const n = Number(value.trim());
  if (!Number.isFinite(n)) {
    throw new UsageError(`bad ${field} ${JSON.stringify(value)} in ${source}`, {
      hint: 'Use a decimal percent, e.g. 3.42.',
    });
  }
  return n;
}

/**
 * Minimal RFC-4180-ish CSV parser: handles quoted fields with embedded commas
 * and doubled quotes. No streaming — fixtures are tiny.
 */
export function parseCsv(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let inQuotes = false;
  for (let i = 0; i < text.length; i += 1) {
    const c = text[i]!;
    if (inQuotes) {
      if (c === '"') {
        if (text[i + 1] === '"') {
          field += '"';
          i += 1;
        } else {
          inQuotes = false;
        }
      } else {
        field += c;
      }
    } else if (c === '"') {
      inQuotes = true;
    } else if (c === ',') {
      row.push(field);
      field = '';
    } else if (c === '\r') {
      // swallow; handled by \n
    } else if (c === '\n') {
      row.push(field);
      rows.push(row);
      row = [];
      field = '';
    } else {
      field += c;
    }
  }
  // last field/row if file didn't end with newline and had content
  if (field.length > 0 || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  // Drop fully empty trailing rows.
  return rows.filter((r) => !(r.length === 1 && r[0] === ''));
}

function readCsvFile(path: string, source: string): string[][] {
  let text: string;
  try {
    text = readFileSync(path, 'utf8');
  } catch (err) {
    throw new UsageError(`could not read ${source} at ${path}`, {
      hint: 'Check the path. Pass --nowcasts / --prints pointing at a CSV file.',
      cause: err,
    });
  }
  return parseCsv(text);
}

function headerIndex(header: string[], name: string, source: string): number {
  const idx = header.findIndex((h) => h.trim() === name);
  if (idx < 0) {
    throw new UsageError(`missing column ${JSON.stringify(name)} in ${source}`, {
      hint: `Expected columns include ${name}.`,
    });
  }
  return idx;
}

/** Load nowcast rows from a CSV with columns: ref_month, vintage_date, nowcast. */
export function loadNowcastsCsv(path: string): NowcastRow[] {
  const rows = readCsvFile(path, 'nowcasts CSV');
  if (rows.length < 2) return [];
  const header = rows[0]!;
  const iRef = headerIndex(header, 'ref_month', 'nowcasts CSV');
  const iVintage = headerIndex(header, 'vintage_date', 'nowcasts CSV');
  const iNow = headerIndex(header, 'nowcast', 'nowcasts CSV');
  const out: NowcastRow[] = [];
  for (let r = 1; r < rows.length; r += 1) {
    const cells = rows[r]!;
    if (cells.length === 1 && cells[0] === '') continue;
    out.push({
      refMonth: parseRefMonth(cells[iRef]!, 'nowcasts CSV'),
      vintageDate: parseDate(cells[iVintage]!, 'vintage_date', 'nowcasts CSV'),
      nowcast: parseFloatField(cells[iNow]!, 'nowcast', 'nowcasts CSV'),
    });
  }
  return out;
}

/** Load print rows from a CSV with columns: ref_month, release_date, yoy. */
export function loadPrintsCsv(path: string): PrintRow[] {
  const rows = readCsvFile(path, 'prints CSV');
  if (rows.length < 2) return [];
  const header = rows[0]!;
  const iRef = headerIndex(header, 'ref_month', 'prints CSV');
  const iRelease = headerIndex(header, 'release_date', 'prints CSV');
  const iYoy = headerIndex(header, 'yoy', 'prints CSV');
  const out: PrintRow[] = [];
  for (let r = 1; r < rows.length; r += 1) {
    const cells = rows[r]!;
    if (cells.length === 1 && cells[0] === '') continue;
    out.push({
      refMonth: parseRefMonth(cells[iRef]!, 'prints CSV'),
      releaseDate: parseDate(cells[iRelease]!, 'release_date', 'prints CSV'),
      yoy: parseFloatField(cells[iYoy]!, 'yoy', 'prints CSV'),
    });
  }
  return out;
}
