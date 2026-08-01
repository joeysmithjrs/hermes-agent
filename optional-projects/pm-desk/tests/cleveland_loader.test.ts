import { readFileSync } from 'node:fs';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { parseNowcastYearJson } from '../src/research/cpi_nowcast/loaders/cleveland_year_json.js';

const SAMPLE = join(
  import.meta.dirname,
  '..',
  'fixtures',
  'cpi_nowcast',
  'live_samples',
  'nowcast_year_sample.json',
);

function loadSample(): unknown {
  return JSON.parse(readFileSync(SAMPLE, 'utf8'));
}

describe('parseNowcastYearJson (Cleveland Fed nowcast_year.json)', () => {
  it('parses the checked-in live sample into nowcast + print rows', () => {
    const { nowcasts, prints } = parseNowcastYearJson(loadSample(), 'test-sample');
    // 4 reference months, 3 completed (with prints) + 1 in-progress (no print yet).
    expect(nowcasts.length).toBeGreaterThan(50);
    expect(prints).toHaveLength(3);
    expect(new Set(prints.map((p) => p.refMonth))).toEqual(new Set(['2026-04', '2026-05', '2026-06']));
  });

  it('every nowcast/print row belongs to a YYYY-MM reference month with real dates', () => {
    const { nowcasts, prints } = parseNowcastYearJson(loadSample(), 'test-sample');
    for (const row of nowcasts) {
      expect(row.refMonth).toMatch(/^\d{4}-\d{2}$/);
      expect(row.vintageDate.getTime()).not.toBeNaN();
    }
    for (const row of prints) {
      expect(row.refMonth).toMatch(/^\d{4}-\d{2}$/);
      expect(row.releaseDate.getTime()).not.toBeNaN();
      expect(Number.isFinite(row.yoy)).toBe(true);
    }
  });

  it('the in-progress reference month (no BLS release yet) yields nowcasts but no print', () => {
    const { nowcasts, prints } = parseNowcastYearJson(loadSample(), 'test-sample');
    const julyNowcasts = nowcasts.filter((n) => n.refMonth === '2026-07');
    const julyPrints = prints.filter((p) => p.refMonth === '2026-07');
    expect(julyNowcasts.length).toBeGreaterThan(0);
    expect(julyPrints).toHaveLength(0);
  });

  it('picks the single actual-print marker point, not a repeated flat line', () => {
    const { prints } = parseNowcastYearJson(loadSample(), 'test-sample');
    const june = prints.filter((p) => p.refMonth === '2026-06');
    expect(june).toHaveLength(1);
  });

  it('vintage dates precede release dates for a completed reference month (no-look-ahead holds)', () => {
    const { nowcasts, prints } = parseNowcastYearJson(loadSample(), 'test-sample');
    const print = prints.find((p) => p.refMonth === '2026-04')!;
    const monthNowcasts = nowcasts.filter((n) => n.refMonth === '2026-04');
    expect(monthNowcasts.length).toBeGreaterThan(0);
    for (const n of monthNowcasts) {
      expect(n.vintageDate.getTime()).toBeLessThan(print.releaseDate.getTime());
    }
  });

  it('resolves a December → January vintage-date rollover from bare MM/DD labels', () => {
    // Synthetic minimal payload: reference month 2025-12, vintages starting
    // 12/29 and continuing into 01/05 the following year — the exact boundary
    // the parser's month-rollover tracking exists to get right.
    const synthetic = [
      {
        chart: { subcaption: '2025-12' },
        categories: [
          {
            category: [{ label: '12/29' }, { label: '12/30' }, { label: '01/02' }, { label: '01/05' }],
          },
        ],
        dataset: [
          {
            seriesname: 'CPI Inflation',
            data: [{ value: '3.1' }, { value: '3.15' }, { value: '3.2' }, { value: '3.25' }],
          },
          {
            seriesname: 'Actual CPI Inflation',
            data: [{ value: '' }, { value: '' }, { value: '' }, { value: '3.3' }],
          },
        ],
      },
    ];
    const { nowcasts, prints } = parseNowcastYearJson(synthetic, 'synthetic');
    expect(nowcasts).toHaveLength(4);
    expect(nowcasts[0]!.vintageDate.getUTCFullYear()).toBe(2025);
    expect(nowcasts[1]!.vintageDate.getUTCFullYear()).toBe(2025);
    expect(nowcasts[2]!.vintageDate.getUTCFullYear()).toBe(2026);
    expect(nowcasts[3]!.vintageDate.getUTCFullYear()).toBe(2026);
    expect(prints).toHaveLength(1);
    expect(prints[0]!.releaseDate.getUTCFullYear()).toBe(2026);
    expect(prints[0]!.releaseDate.getUTCMonth()).toBe(0); // January
    expect(prints[0]!.releaseDate.getUTCDate()).toBe(5);
  });

  it('skips vline marker labels ("CPI Mar") without treating them as dates', () => {
    const synthetic = [
      {
        chart: { subcaption: '2026-01' },
        categories: [
          {
            category: [
              { label: '01/05' },
              { label: 'CPI Dec', vline: 'true' },
              { label: '01/06' },
            ],
          },
        ],
        dataset: [
          { seriesname: 'CPI Inflation', data: [{ value: '3.0' }, { value: '3.0' }, { value: '3.1' }] },
          { seriesname: 'Actual CPI Inflation', data: [{ value: '' }, { value: '' }, { value: '' }] },
        ],
      },
    ];
    const { nowcasts } = parseNowcastYearJson(synthetic, 'synthetic');
    expect(nowcasts).toHaveLength(2);
    expect(nowcasts.map((n) => n.vintageDate.getUTCDate())).toEqual([5, 6]);
  });

  it('skips a chart entry missing the two series it reads, without failing the whole feed', () => {
    const synthetic = [
      { chart: { subcaption: '2026-01' }, categories: [{ category: [{ label: '01/05' }] }], dataset: [] },
      {
        chart: { subcaption: '2026-02' },
        categories: [{ category: [{ label: '02/05' }] }],
        dataset: [
          { seriesname: 'CPI Inflation', data: [{ value: '3.0' }] },
          { seriesname: 'Actual CPI Inflation', data: [{ value: '' }] },
        ],
      },
    ];
    const { nowcasts } = parseNowcastYearJson(synthetic, 'synthetic');
    expect(nowcasts).toHaveLength(1);
    expect(nowcasts[0]!.refMonth).toBe('2026-02');
  });

  it('rejects a payload that is not an array', () => {
    expect(() => parseNowcastYearJson({ not: 'an array' }, 'bad-source')).toThrow(/not a JSON array/);
  });
});
