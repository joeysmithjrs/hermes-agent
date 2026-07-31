/** Output helpers. `--json` mode prints machine-readable JSON and nothing else. */

export interface OutputMode {
  json: boolean;
}

export function emit(mode: OutputMode, payload: unknown, human: () => string): void {
  if (mode.json) {
    process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
  } else {
    process.stdout.write(`${human()}\n`);
  }
}

export function table(rows: Record<string, unknown>[], columns: string[]): string {
  if (rows.length === 0) return '(none)';
  const widths = columns.map((col) =>
    Math.max(col.length, ...rows.map((row) => String(row[col] ?? '').length)),
  );
  const line = (cells: string[]) =>
    cells.map((cell, i) => cell.padEnd(widths[i] ?? cell.length)).join('  ');
  return [
    line(columns),
    line(widths.map((w) => '-'.repeat(w))),
    ...rows.map((row) => line(columns.map((col) => String(row[col] ?? '')))),
  ].join('\n');
}
