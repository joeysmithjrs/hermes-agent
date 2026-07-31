import type Database from 'better-sqlite3';

import { LedgerInvariantError } from '../core/errors.js';
import { nowIso } from '../core/time.js';
import type { LedgerAnnotation, LedgerAnnotationInput, PaperLedgerEntry } from './types.js';

export class PaperLedgerRepository {
  constructor(private readonly db: Database.Database) {}

  insert(entry: PaperLedgerEntry): PaperLedgerEntry {
    this.db
      .prepare(
        `INSERT INTO paper_ledger (
           entry_id, signal_id, origin, thesis, thesis_hash, decided_at, market_id,
           condition_id, token_id, outcome, entry_observed_at, entry_mid, entry_best_bid,
           entry_best_ask, entry_spread, assumed_size_usd, slippage_rule, assumed_entry_price,
           expiry_horizon_s, expires_at, markout_horizons_json, invalidations_json,
           evidence_refs_json, paper_only, fill_type, created_at
         ) VALUES (
           @entry_id, @signal_id, @origin, @thesis, @thesis_hash, @decided_at, @market_id,
           @condition_id, @token_id, @outcome, @entry_observed_at, @entry_mid, @entry_best_bid,
           @entry_best_ask, @entry_spread, @assumed_size_usd, @slippage_rule, @assumed_entry_price,
           @expiry_horizon_s, @expires_at, @markout_horizons_json, @invalidations_json,
           @evidence_refs_json, 1, 'SIMULATED_NO_FILL', @created_at
         )`,
      )
      .run({
        ...entry,
        markout_horizons_json: JSON.stringify(entry.markout_horizons_s),
        invalidations_json: JSON.stringify(entry.invalidations),
        evidence_refs_json: JSON.stringify(entry.evidence_refs),
      });
    return entry;
  }

  get(entryId: string): PaperLedgerEntry | undefined {
    const row = this.db.prepare('SELECT * FROM paper_ledger WHERE entry_id = ?').get(entryId) as
      Record<string, unknown> | undefined;
    return row ? mapEntry(row) : undefined;
  }

  findBySignal(signalId: string): PaperLedgerEntry | undefined {
    const row = this.db
      .prepare('SELECT * FROM paper_ledger WHERE signal_id = ? ORDER BY created_at DESC LIMIT 1')
      .get(signalId) as Record<string, unknown> | undefined;
    return row ? mapEntry(row) : undefined;
  }

  list(limit = 50): PaperLedgerEntry[] {
    return (
      this.db
        .prepare('SELECT * FROM paper_ledger ORDER BY decided_at DESC LIMIT ?')
        .all(limit) as Record<string, unknown>[]
    ).map(mapEntry);
  }

  count(): number {
    return (this.db.prepare('SELECT COUNT(*) AS n FROM paper_ledger').get() as { n: number }).n;
  }

  annotate(input: LedgerAnnotationInput): number {
    if (!this.get(input.entry_id)) {
      throw new LedgerInvariantError(`no paper ledger entry ${input.entry_id}`, {
        hint: 'List entries with `pm-desk ledger list` and use an entry_id from there.',
      });
    }
    const result = this.db
      .prepare(
        `INSERT INTO paper_ledger_annotations (entry_id, recorded_at, kind, note, detail_json)
         VALUES (?, ?, ?, ?, ?)`,
      )
      .run(
        input.entry_id,
        input.recorded_at || nowIso(),
        input.kind,
        input.note ?? null,
        JSON.stringify(input.detail ?? null),
      );
    return Number(result.lastInsertRowid);
  }

  listAnnotations(entryId: string): LedgerAnnotation[] {
    return (
      this.db
        .prepare('SELECT * FROM paper_ledger_annotations WHERE entry_id = ? ORDER BY id ASC')
        .all(entryId) as Record<string, unknown>[]
    ).map((row) => ({
      id: Number(row.id),
      entry_id: String(row.entry_id),
      recorded_at: String(row.recorded_at),
      kind: row.kind as LedgerAnnotation['kind'],
      note: (row.note as string | undefined) ?? undefined,
      detail: typeof row.detail_json === 'string' ? JSON.parse(row.detail_json) : null,
    }));
  }
}

function mapEntry(row: Record<string, unknown>): PaperLedgerEntry {
  return {
    entry_id: String(row.entry_id),
    signal_id: (row.signal_id as string | null) ?? null,
    origin: row.origin as PaperLedgerEntry['origin'],
    thesis: String(row.thesis),
    thesis_hash: String(row.thesis_hash),
    decided_at: String(row.decided_at),
    market_id: (row.market_id as string | null) ?? null,
    condition_id: (row.condition_id as string | null) ?? null,
    token_id: (row.token_id as string | null) ?? null,
    outcome: (row.outcome as 'YES' | 'NO' | null) ?? null,
    entry_observed_at: (row.entry_observed_at as string | null) ?? null,
    entry_mid: (row.entry_mid as number | null) ?? null,
    entry_best_bid: (row.entry_best_bid as number | null) ?? null,
    entry_best_ask: (row.entry_best_ask as number | null) ?? null,
    entry_spread: (row.entry_spread as number | null) ?? null,
    assumed_size_usd: Number(row.assumed_size_usd),
    slippage_rule: row.slippage_rule as PaperLedgerEntry['slippage_rule'],
    assumed_entry_price: (row.assumed_entry_price as number | null) ?? null,
    expiry_horizon_s: Number(row.expiry_horizon_s),
    expires_at: String(row.expires_at),
    markout_horizons_s: JSON.parse(String(row.markout_horizons_json)) as number[],
    invalidations: JSON.parse(String(row.invalidations_json)) as string[],
    evidence_refs: JSON.parse(String(row.evidence_refs_json)) as string[],
    paper_only: true,
    fill_type: 'SIMULATED_NO_FILL',
    created_at: String(row.created_at),
  };
}
