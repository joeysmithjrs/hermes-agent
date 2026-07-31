import type Database from 'better-sqlite3';

import { StoreError } from '../core/errors.js';
import { nowIso, type IsoTimestamp } from '../core/time.js';
import { parseSignalEnvelope, type SignalEnvelope } from '../schema/signal.js';
import type { ArtifactStore } from './artifacts.js';
import type {
  MarketRow,
  MarketUpsert,
  MonitorDecisionInput,
  MonitorDecisionRow,
  MonitorStateRow,
  ObservationInput,
  ObservationRow,
  OutboxRow,
  OutboxStatus,
  SnapshotInput,
  SnapshotRow,
  TokenBinding,
  TokenRow,
  TokenUpsert,
} from './types.js';

const bool = (v: unknown): boolean => v === 1 || v === true;
const int = (v: boolean | undefined): number => (v ? 1 : 0);
const json = (v: unknown): string => JSON.stringify(v ?? null);
const unjson = (v: unknown): unknown => (typeof v === 'string' ? JSON.parse(v) : null);

export class MarketRepository {
  constructor(private readonly db: Database.Database) {}

  upsertMarket(input: MarketUpsert): void {
    this.db
      .prepare(
        `INSERT INTO markets (
           market_id, condition_id, question, slug, category, active, closed,
           accepting_orders, neg_risk, start_date, end_date, resolution_source,
           event_id, event_title, raw_json, first_seen_at, updated_at
         ) VALUES (
           @market_id, @condition_id, @question, @slug, @category, @active, @closed,
           @accepting_orders, @neg_risk, @start_date, @end_date, @resolution_source,
           @event_id, @event_title, @raw_json, @observed_at, @observed_at
         )
         ON CONFLICT(market_id) DO UPDATE SET
           condition_id = excluded.condition_id,
           question = excluded.question,
           slug = excluded.slug,
           category = excluded.category,
           active = excluded.active,
           closed = excluded.closed,
           accepting_orders = excluded.accepting_orders,
           neg_risk = excluded.neg_risk,
           start_date = excluded.start_date,
           end_date = excluded.end_date,
           resolution_source = excluded.resolution_source,
           event_id = excluded.event_id,
           event_title = excluded.event_title,
           raw_json = excluded.raw_json,
           updated_at = excluded.updated_at`,
      )
      .run({
        market_id: input.market_id,
        condition_id: input.condition_id ?? null,
        question: input.question,
        slug: input.slug ?? null,
        category: input.category ?? null,
        active: int(input.active),
        closed: int(input.closed),
        accepting_orders: int(input.accepting_orders),
        neg_risk: int(input.neg_risk),
        start_date: input.start_date ?? null,
        end_date: input.end_date ?? null,
        resolution_source: input.resolution_source ?? null,
        event_id: input.event_id ?? null,
        event_title: input.event_title ?? null,
        raw_json: json(input.raw),
        observed_at: input.observed_at,
      });
  }

  upsertToken(input: TokenUpsert): void {
    this.db
      .prepare(
        `INSERT INTO outcome_tokens (token_id, market_id, outcome, label, updated_at)
         VALUES (?, ?, ?, ?, ?)
         ON CONFLICT(token_id) DO UPDATE SET
           market_id = excluded.market_id,
           outcome = excluded.outcome,
           label = excluded.label,
           updated_at = excluded.updated_at`,
      )
      .run(input.token_id, input.market_id, input.outcome, input.label ?? null, input.observed_at);
  }

  getMarket(marketId: string): MarketRow | undefined {
    const row = this.db.prepare('SELECT * FROM markets WHERE market_id = ?').get(marketId) as
      Record<string, unknown> | undefined;
    return row ? mapMarket(row) : undefined;
  }

  getMarketRaw(marketId: string): unknown {
    const row = this.db
      .prepare('SELECT raw_json FROM markets WHERE market_id = ?')
      .get(marketId) as { raw_json: string } | undefined;
    return row ? JSON.parse(row.raw_json) : undefined;
  }

  listTokens(marketId: string): TokenRow[] {
    return (
      this.db
        .prepare('SELECT * FROM outcome_tokens WHERE market_id = ? ORDER BY outcome')
        .all(marketId) as Record<string, unknown>[]
    ).map((row) => ({
      token_id: String(row.token_id),
      market_id: String(row.market_id),
      outcome: row.outcome as 'YES' | 'NO',
      label: (row.label as string | null) ?? null,
      updated_at: String(row.updated_at),
    }));
  }

  getTokenBinding(tokenId: string): TokenBinding | undefined {
    const row = this.db
      .prepare(
        `SELECT t.token_id, t.market_id, t.outcome, m.condition_id, m.question, m.end_date,
                m.active, m.closed
         FROM outcome_tokens t
         JOIN markets m ON m.market_id = t.market_id
         WHERE t.token_id = ?`,
      )
      .get(tokenId) as Record<string, unknown> | undefined;
    if (!row) return undefined;
    return {
      token_id: String(row.token_id),
      market_id: String(row.market_id),
      condition_id: (row.condition_id as string | null) ?? null,
      outcome: row.outcome as 'YES' | 'NO',
      question: String(row.question),
      end_date: (row.end_date as string | null) ?? null,
      active: bool(row.active),
      closed: bool(row.closed),
    };
  }

  listMarkets(options: { limit?: number; openOnly?: boolean } = {}): MarketRow[] {
    const where = options.openOnly ? 'WHERE closed = 0' : '';
    const rows = this.db
      .prepare(`SELECT * FROM markets ${where} ORDER BY updated_at DESC LIMIT ?`)
      .all(options.limit ?? 100) as Record<string, unknown>[];
    return rows.map(mapMarket);
  }

  countMarkets(): number {
    return (this.db.prepare('SELECT COUNT(*) AS n FROM markets').get() as { n: number }).n;
  }
}

function mapMarket(row: Record<string, unknown>): MarketRow {
  return {
    market_id: String(row.market_id),
    condition_id: (row.condition_id as string | null) ?? null,
    question: String(row.question),
    slug: (row.slug as string | null) ?? null,
    category: (row.category as string | null) ?? null,
    active: bool(row.active),
    closed: bool(row.closed),
    accepting_orders: bool(row.accepting_orders),
    neg_risk: bool(row.neg_risk),
    start_date: (row.start_date as string | null) ?? null,
    end_date: (row.end_date as string | null) ?? null,
    resolution_source: (row.resolution_source as string | null) ?? null,
    event_id: (row.event_id as string | null) ?? null,
    event_title: (row.event_title as string | null) ?? null,
    first_seen_at: String(row.first_seen_at),
    updated_at: String(row.updated_at),
  };
}

export class ObservationRepository {
  constructor(private readonly db: Database.Database) {}

  append(input: ObservationInput): number {
    const result = this.db
      .prepare(
        `INSERT INTO market_observations (
           token_id, market_id, observed_at, mid, best_bid, best_ask, spread,
           last_trade_price, book_available, bid_depth, ask_depth, source, raw_json
         ) VALUES (
           @token_id, @market_id, @observed_at, @mid, @best_bid, @best_ask, @spread,
           @last_trade_price, @book_available, @bid_depth, @ask_depth, @source, @raw_json
         )`,
      )
      .run({
        token_id: input.token_id,
        market_id: input.market_id ?? null,
        observed_at: input.observed_at,
        mid: input.mid ?? null,
        best_bid: input.best_bid ?? null,
        best_ask: input.best_ask ?? null,
        spread: input.spread ?? null,
        last_trade_price: input.last_trade_price ?? null,
        book_available: int(input.book_available),
        bid_depth: input.bid_depth ?? null,
        ask_depth: input.ask_depth ?? null,
        source: input.source ?? 'polymarket_public_sdk',
        raw_json: input.raw === undefined ? null : json(input.raw),
      });
    return Number(result.lastInsertRowid);
  }

  latest(tokenId: string): ObservationRow | undefined {
    const row = this.db
      .prepare(
        `SELECT * FROM market_observations WHERE token_id = ?
         ORDER BY observed_at DESC, id DESC LIMIT 1`,
      )
      .get(tokenId) as Record<string, unknown> | undefined;
    return row ? mapObservation(row) : undefined;
  }

  /** Most recent observation at or before `boundary` — the move-rule reference point. */
  latestAtOrBefore(tokenId: string, boundary: IsoTimestamp): ObservationRow | undefined {
    const row = this.db
      .prepare(
        `SELECT * FROM market_observations WHERE token_id = ? AND observed_at <= ?
         ORDER BY observed_at DESC, id DESC LIMIT 1`,
      )
      .get(tokenId, boundary) as Record<string, unknown> | undefined;
    return row ? mapObservation(row) : undefined;
  }

  listSince(tokenId: string, since: IsoTimestamp, limit = 500): ObservationRow[] {
    return (
      this.db
        .prepare(
          `SELECT * FROM market_observations WHERE token_id = ? AND observed_at >= ?
           ORDER BY observed_at ASC LIMIT ?`,
        )
        .all(tokenId, since, limit) as Record<string, unknown>[]
    ).map(mapObservation);
  }

  count(tokenId?: string): number {
    const row = tokenId
      ? (this.db
          .prepare('SELECT COUNT(*) AS n FROM market_observations WHERE token_id = ?')
          .get(tokenId) as { n: number })
      : (this.db.prepare('SELECT COUNT(*) AS n FROM market_observations').get() as { n: number });
    return row.n;
  }
}

function mapObservation(row: Record<string, unknown>): ObservationRow {
  return {
    id: Number(row.id),
    token_id: String(row.token_id),
    market_id: (row.market_id as string | null) ?? null,
    observed_at: String(row.observed_at),
    mid: (row.mid as number | null) ?? null,
    best_bid: (row.best_bid as number | null) ?? null,
    best_ask: (row.best_ask as number | null) ?? null,
    spread: (row.spread as number | null) ?? null,
    last_trade_price: (row.last_trade_price as number | null) ?? null,
    book_available: bool(row.book_available),
    bid_depth: (row.bid_depth as number | null) ?? null,
    ask_depth: (row.ask_depth as number | null) ?? null,
    source: String(row.source),
  };
}

export class SourceRepository {
  constructor(
    private readonly db: Database.Database,
    private readonly artifacts: ArtifactStore,
  ) {}

  /**
   * Records a collection. `changed` compares against the most recent snapshot for
   * the same source: the first-ever collection counts as a change (nothing → something),
   * a re-collection of identical content does not.
   */
  record(input: SnapshotInput): SnapshotRow {
    const previous = this.latest(input.source_id);
    const previousHash = previous?.content_hash ?? null;
    const changed = previousHash !== input.content_hash;
    const normalized = this.artifacts.put(input.normalized_text, 'text/plain');

    const result = this.db
      .prepare(
        `INSERT INTO source_snapshots (
           source_id, spec_version, url, collected_at, content_hash, previous_hash,
           changed, normalized_artifact_ref, raw_artifact_ref, fields_json, collector, mode
         ) VALUES (
           @source_id, @spec_version, @url, @collected_at, @content_hash, @previous_hash,
           @changed, @normalized_artifact_ref, @raw_artifact_ref, @fields_json, @collector, @mode
         )`,
      )
      .run({
        source_id: input.source_id,
        spec_version: input.spec_version,
        url: input.url,
        collected_at: input.collected_at,
        content_hash: input.content_hash,
        previous_hash: previousHash,
        changed: int(changed),
        normalized_artifact_ref: normalized.ref,
        raw_artifact_ref: input.raw_artifact_ref ?? null,
        fields_json: json(input.fields ?? {}),
        collector: input.collector ?? 'browserbase',
        mode: input.mode ?? 'fixture',
      });

    const row = this.getById(Number(result.lastInsertRowid));
    if (!row) throw new StoreError('failed to read back the snapshot just written');
    return row;
  }

  getById(id: number): SnapshotRow | undefined {
    const row = this.db.prepare('SELECT * FROM source_snapshots WHERE id = ?').get(id) as
      Record<string, unknown> | undefined;
    return row ? mapSnapshot(row) : undefined;
  }

  latest(sourceId: string): SnapshotRow | undefined {
    const row = this.db
      .prepare(
        `SELECT * FROM source_snapshots WHERE source_id = ?
         ORDER BY collected_at DESC, id DESC LIMIT 1`,
      )
      .get(sourceId) as Record<string, unknown> | undefined;
    return row ? mapSnapshot(row) : undefined;
  }

  /**
   * The snapshot that established the *previous* content, i.e. the last row whose
   * content_hash differs from the current one. That is the correct diff baseline
   * even when a source has been polled unchanged many times in between.
   */
  previousChanged(sourceId: string): SnapshotRow | undefined {
    const latest = this.latest(sourceId);
    if (!latest) return undefined;
    const row = this.db
      .prepare(
        `SELECT * FROM source_snapshots
         WHERE source_id = ? AND content_hash != ? AND id < ?
         ORDER BY id DESC LIMIT 1`,
      )
      .get(sourceId, latest.content_hash, latest.id) as Record<string, unknown> | undefined;
    return row ? mapSnapshot(row) : undefined;
  }

  listSources(): string[] {
    return (
      this.db
        .prepare('SELECT DISTINCT source_id FROM source_snapshots ORDER BY source_id')
        .all() as { source_id: string }[]
    ).map((r) => r.source_id);
  }

  history(sourceId: string, limit = 50): SnapshotRow[] {
    return (
      this.db
        .prepare('SELECT * FROM source_snapshots WHERE source_id = ? ORDER BY id DESC LIMIT ?')
        .all(sourceId, limit) as Record<string, unknown>[]
    ).map(mapSnapshot);
  }
}

function mapSnapshot(row: Record<string, unknown>): SnapshotRow {
  return {
    id: Number(row.id),
    source_id: String(row.source_id),
    spec_version: Number(row.spec_version),
    url: String(row.url),
    collected_at: String(row.collected_at),
    content_hash: String(row.content_hash),
    previous_hash: (row.previous_hash as string | null) ?? null,
    changed: bool(row.changed),
    normalized_artifact_ref: String(row.normalized_artifact_ref),
    raw_artifact_ref: (row.raw_artifact_ref as string | null) ?? null,
    fields: (unjson(row.fields_json) as Record<string, string | null>) ?? {},
    collector: String(row.collector),
    mode: String(row.mode),
  };
}

export interface SignalRecordResult {
  inserted: boolean;
  signal_id: string;
  /** Set when a duplicate was rejected: the id of the signal already stored. */
  existing_signal_id?: string;
}

export class SignalRepository {
  constructor(private readonly db: Database.Database) {}

  /**
   * Records first, dispatches later. Idempotent on both `signal_id` and
   * `dedupe_key`: a replayed POST and a re-derived twin with a fresh id both
   * collapse onto the stored row.
   */
  record(envelope: SignalEnvelope, origin: string): SignalRecordResult {
    const valid = parseSignalEnvelope(envelope);
    const existing = this.get(valid.signal_id) ?? this.getByDedupeKey(valid.dedupe_key);
    if (existing) {
      return {
        inserted: false,
        signal_id: valid.signal_id,
        existing_signal_id: existing.envelope.signal_id,
      };
    }
    this.db
      .prepare(
        `INSERT INTO signals (
           signal_id, dedupe_key, kind, severity, observed_at, rule_id, rule_version,
           envelope_json, origin, recorded_at, paper_only
         ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)`,
      )
      .run(
        valid.signal_id,
        valid.dedupe_key,
        valid.kind,
        valid.severity,
        valid.observed_at,
        valid.rule_id,
        valid.rule_version,
        JSON.stringify(valid),
        origin,
        nowIso(),
      );
    return { inserted: true, signal_id: valid.signal_id };
  }

  get(
    signalId: string,
  ): { envelope: SignalEnvelope; origin: string; recorded_at: string } | undefined {
    const row = this.db.prepare('SELECT * FROM signals WHERE signal_id = ?').get(signalId) as
      Record<string, unknown> | undefined;
    return row ? mapSignal(row) : undefined;
  }

  getByDedupeKey(
    dedupeKey: string,
  ): { envelope: SignalEnvelope; origin: string; recorded_at: string } | undefined {
    const row = this.db.prepare('SELECT * FROM signals WHERE dedupe_key = ?').get(dedupeKey) as
      Record<string, unknown> | undefined;
    return row ? mapSignal(row) : undefined;
  }

  list(limit = 50): { envelope: SignalEnvelope; origin: string; recorded_at: string }[] {
    return (
      this.db
        .prepare('SELECT * FROM signals ORDER BY recorded_at DESC, rowid DESC LIMIT ?')
        .all(limit) as Record<string, unknown>[]
    ).map(mapSignal);
  }

  count(): number {
    return (this.db.prepare('SELECT COUNT(*) AS n FROM signals').get() as { n: number }).n;
  }

  // --- outbox -------------------------------------------------------------

  enqueue(signalId: string, dispatcher: string, artifactRef?: string, detail?: unknown): number {
    const result = this.db
      .prepare(
        `INSERT INTO signal_outbox (signal_id, queued_at, status, dispatcher, artifact_ref, detail_json)
         VALUES (?, ?, 'queued', ?, ?, ?)`,
      )
      .run(signalId, nowIso(), dispatcher, artifactRef ?? null, json(detail));
    return Number(result.lastInsertRowid);
  }

  /**
   * Whether this signal has ever been handed to a dispatcher. Dispatch
   * idempotency keys on this rather than on mere existence, because the monitor
   * records a signal at emission time — that must not consume its one dispatch.
   */
  hasOutboxFor(signalId: string): boolean {
    const row = this.db
      .prepare('SELECT 1 AS present FROM signal_outbox WHERE signal_id = ? LIMIT 1')
      .get(signalId) as { present: number } | undefined;
    return row !== undefined;
  }

  markOutbox(id: number, status: OutboxStatus, detail?: unknown): void {
    this.db
      .prepare('UPDATE signal_outbox SET status = ?, detail_json = ? WHERE id = ?')
      .run(status, json(detail), id);
  }

  listOutbox(status?: OutboxStatus, limit = 50): OutboxRow[] {
    const rows = status
      ? (this.db
          .prepare('SELECT * FROM signal_outbox WHERE status = ? ORDER BY id DESC LIMIT ?')
          .all(status, limit) as Record<string, unknown>[])
      : (this.db
          .prepare('SELECT * FROM signal_outbox ORDER BY id DESC LIMIT ?')
          .all(limit) as Record<string, unknown>[]);
    return rows.map((row) => ({
      id: Number(row.id),
      signal_id: String(row.signal_id),
      queued_at: String(row.queued_at),
      status: row.status as OutboxStatus,
      dispatcher: String(row.dispatcher),
      artifact_ref: (row.artifact_ref as string | null) ?? null,
      detail: unjson(row.detail_json),
    }));
  }
}

function mapSignal(row: Record<string, unknown>) {
  return {
    envelope: parseSignalEnvelope(JSON.parse(String(row.envelope_json))),
    origin: String(row.origin),
    recorded_at: String(row.recorded_at),
  };
}

export class MonitorStateRepository {
  constructor(private readonly db: Database.Database) {}

  get(ruleId: string, dedupeKey: string): MonitorStateRow | undefined {
    const row = this.db
      .prepare('SELECT * FROM monitor_state WHERE rule_id = ? AND dedupe_key = ?')
      .get(ruleId, dedupeKey) as Record<string, unknown> | undefined;
    if (!row) return undefined;
    return {
      rule_id: String(row.rule_id),
      dedupe_key: String(row.dedupe_key),
      last_emitted_at: String(row.last_emitted_at),
      last_signal_id: (row.last_signal_id as string | null) ?? null,
      last_value: unjson(row.last_value_json),
    };
  }

  /** Most recent emission for a rule regardless of key — drives per-rule cooldown. */
  lastEmissionForRule(ruleId: string): MonitorStateRow | undefined {
    const row = this.db
      .prepare(
        'SELECT * FROM monitor_state WHERE rule_id = ? ORDER BY last_emitted_at DESC LIMIT 1',
      )
      .get(ruleId) as Record<string, unknown> | undefined;
    if (!row) return undefined;
    return {
      rule_id: String(row.rule_id),
      dedupe_key: String(row.dedupe_key),
      last_emitted_at: String(row.last_emitted_at),
      last_signal_id: (row.last_signal_id as string | null) ?? null,
      last_value: unjson(row.last_value_json),
    };
  }

  markEmitted(
    ruleId: string,
    dedupeKey: string,
    at: IsoTimestamp,
    signalId?: string,
    lastValue?: unknown,
  ): void {
    this.db
      .prepare(
        `INSERT INTO monitor_state (rule_id, dedupe_key, last_emitted_at, last_signal_id, last_value_json)
         VALUES (?, ?, ?, ?, ?)
         ON CONFLICT(rule_id, dedupe_key) DO UPDATE SET
           last_emitted_at = excluded.last_emitted_at,
           last_signal_id = excluded.last_signal_id,
           last_value_json = excluded.last_value_json`,
      )
      .run(ruleId, dedupeKey, at, signalId ?? null, json(lastValue));
  }

  recordDecision(input: MonitorDecisionInput): number {
    const result = this.db
      .prepare(
        `INSERT INTO monitor_decisions
           (rule_id, rule_version, evaluated_at, outcome, dedupe_key, signal_id, reason, detail_json)
         VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
      )
      .run(
        input.rule_id,
        input.rule_version,
        input.evaluated_at,
        input.outcome,
        input.dedupe_key ?? null,
        input.signal_id ?? null,
        input.reason ?? null,
        json(input.detail),
      );
    return Number(result.lastInsertRowid);
  }

  listDecisions(ruleId?: string, limit = 100): MonitorDecisionRow[] {
    const rows = ruleId
      ? (this.db
          .prepare('SELECT * FROM monitor_decisions WHERE rule_id = ? ORDER BY id ASC LIMIT ?')
          .all(ruleId, limit) as Record<string, unknown>[])
      : (this.db
          .prepare('SELECT * FROM monitor_decisions ORDER BY id ASC LIMIT ?')
          .all(limit) as Record<string, unknown>[]);
    return rows.map((row) => ({
      id: Number(row.id),
      rule_id: String(row.rule_id),
      rule_version: String(row.rule_version),
      evaluated_at: String(row.evaluated_at),
      outcome: row.outcome as MonitorDecisionRow['outcome'],
      dedupe_key: (row.dedupe_key as string | undefined) ?? undefined,
      signal_id: (row.signal_id as string | undefined) ?? undefined,
      reason: (row.reason as string | undefined) ?? undefined,
      detail: unjson(row.detail_json),
    }));
  }
}

export class AdjudicationRepository {
  constructor(private readonly db: Database.Database) {}

  /**
   * One adjudication per signal. A second attempt is a conflict rather than an
   * overwrite, so a re-run cannot silently replace a recorded decision.
   */
  record(adjudication: { signal_id: string; decision: string }, recordedAt: IsoTimestamp): void {
    this.db
      .prepare(
        `INSERT INTO adjudications (signal_id, decision, adjudication_json, recorded_at)
         VALUES (?, ?, ?, ?)
         ON CONFLICT(signal_id) DO NOTHING`,
      )
      .run(adjudication.signal_id, adjudication.decision, JSON.stringify(adjudication), recordedAt);
  }

  get(
    signalId: string,
  ): { decision: string; adjudication: unknown; recorded_at: string } | undefined {
    const row = this.db.prepare('SELECT * FROM adjudications WHERE signal_id = ?').get(signalId) as
      Record<string, unknown> | undefined;
    if (!row) return undefined;
    return {
      decision: String(row.decision),
      adjudication: JSON.parse(String(row.adjudication_json)),
      recorded_at: String(row.recorded_at),
    };
  }

  list(limit = 50): { signal_id: string; decision: string; recorded_at: string }[] {
    return (
      this.db
        .prepare('SELECT * FROM adjudications ORDER BY recorded_at DESC LIMIT ?')
        .all(limit) as Record<string, unknown>[]
    ).map((row) => ({
      signal_id: String(row.signal_id),
      decision: String(row.decision),
      recorded_at: String(row.recorded_at),
    }));
  }
}
