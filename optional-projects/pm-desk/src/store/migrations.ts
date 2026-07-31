import type Database from 'better-sqlite3';

/**
 * Forward-only, numbered migrations. Each entry runs inside a transaction and is
 * recorded in `schema_migrations`, so `openStore` on an existing home is a no-op
 * and never rewrites data.
 *
 * Storage conventions:
 * - every timestamp column is UTC ISO-8601 with millisecond precision (TEXT);
 * - booleans are INTEGER 0/1;
 * - "missing data" is NULL, never 0 — a missing book must not read as a 0.0 mid;
 * - bodies live in the content-addressed artifact store, rows keep the hash.
 */
export interface Migration {
  version: number;
  name: string;
  up: string;
}

export const MIGRATIONS: readonly Migration[] = [
  {
    version: 1,
    name: 'initial_desk_schema',
    up: `
      CREATE TABLE markets (
        market_id         TEXT PRIMARY KEY,
        condition_id      TEXT,
        question          TEXT NOT NULL,
        slug              TEXT,
        category          TEXT,
        active            INTEGER NOT NULL DEFAULT 0,
        closed            INTEGER NOT NULL DEFAULT 0,
        accepting_orders  INTEGER NOT NULL DEFAULT 0,
        neg_risk          INTEGER NOT NULL DEFAULT 0,
        start_date        TEXT,
        end_date          TEXT,
        resolution_source TEXT,
        event_id          TEXT,
        event_title       TEXT,
        raw_json          TEXT NOT NULL,
        first_seen_at     TEXT NOT NULL,
        updated_at        TEXT NOT NULL
      );
      CREATE INDEX idx_markets_condition ON markets(condition_id);
      CREATE INDEX idx_markets_end_date ON markets(end_date);

      CREATE TABLE outcome_tokens (
        token_id    TEXT PRIMARY KEY,
        market_id   TEXT NOT NULL REFERENCES markets(market_id),
        outcome     TEXT NOT NULL CHECK (outcome IN ('YES','NO')),
        label       TEXT,
        updated_at  TEXT NOT NULL
      );
      CREATE INDEX idx_tokens_market ON outcome_tokens(market_id);

      CREATE TABLE market_observations (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        token_id         TEXT NOT NULL,
        market_id        TEXT,
        observed_at      TEXT NOT NULL,
        mid              REAL,
        best_bid         REAL,
        best_ask         REAL,
        spread           REAL,
        last_trade_price REAL,
        book_available   INTEGER NOT NULL DEFAULT 0,
        bid_depth        REAL,
        ask_depth        REAL,
        source           TEXT NOT NULL DEFAULT 'polymarket_public_sdk',
        raw_json         TEXT
      );
      CREATE INDEX idx_obs_token_time ON market_observations(token_id, observed_at);

      CREATE TABLE artifacts (
        hash        TEXT PRIMARY KEY,
        rel_path    TEXT NOT NULL,
        bytes       INTEGER NOT NULL,
        media_type  TEXT NOT NULL,
        created_at  TEXT NOT NULL
      );

      CREATE TABLE source_snapshots (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        source_id              TEXT NOT NULL,
        spec_version           INTEGER NOT NULL,
        url                    TEXT NOT NULL,
        collected_at           TEXT NOT NULL,
        content_hash           TEXT NOT NULL,
        previous_hash          TEXT,
        changed                INTEGER NOT NULL,
        normalized_artifact_ref TEXT NOT NULL,
        raw_artifact_ref       TEXT,
        fields_json            TEXT NOT NULL DEFAULT '{}',
        collector              TEXT NOT NULL DEFAULT 'browserbase',
        mode                   TEXT NOT NULL DEFAULT 'fixture'
      );
      CREATE INDEX idx_snap_source_time ON source_snapshots(source_id, collected_at);
      CREATE INDEX idx_snap_source_changed ON source_snapshots(source_id, changed, collected_at);

      CREATE TABLE monitor_state (
        rule_id         TEXT NOT NULL,
        dedupe_key      TEXT NOT NULL,
        last_emitted_at TEXT NOT NULL,
        last_signal_id  TEXT,
        last_value_json TEXT,
        PRIMARY KEY (rule_id, dedupe_key)
      );

      CREATE TABLE monitor_decisions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        rule_id      TEXT NOT NULL,
        rule_version TEXT NOT NULL,
        evaluated_at TEXT NOT NULL,
        outcome      TEXT NOT NULL,
        dedupe_key   TEXT,
        signal_id    TEXT,
        reason       TEXT,
        detail_json  TEXT
      );
      CREATE INDEX idx_decisions_rule ON monitor_decisions(rule_id, id);

      CREATE TABLE signals (
        signal_id     TEXT PRIMARY KEY,
        dedupe_key    TEXT NOT NULL UNIQUE,
        kind          TEXT NOT NULL,
        severity      TEXT NOT NULL,
        observed_at   TEXT NOT NULL,
        rule_id       TEXT NOT NULL,
        rule_version  TEXT NOT NULL,
        envelope_json TEXT NOT NULL,
        origin        TEXT NOT NULL,
        recorded_at   TEXT NOT NULL,
        paper_only    INTEGER NOT NULL DEFAULT 1 CHECK (paper_only = 1)
      );
      CREATE INDEX idx_signals_observed ON signals(observed_at);

      CREATE TABLE signal_outbox (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id     TEXT NOT NULL REFERENCES signals(signal_id),
        queued_at     TEXT NOT NULL,
        status        TEXT NOT NULL CHECK (status IN ('queued','dispatched','failed')),
        dispatcher    TEXT NOT NULL,
        artifact_ref  TEXT,
        detail_json   TEXT
      );
      CREATE INDEX idx_outbox_status ON signal_outbox(status, id);

      CREATE TABLE adjudications (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        signal_id          TEXT NOT NULL REFERENCES signals(signal_id),
        decision           TEXT NOT NULL CHECK (decision IN ('ignore','watch','paper_alert')),
        adjudication_json  TEXT NOT NULL,
        recorded_at        TEXT NOT NULL,
        UNIQUE (signal_id)
      );

      CREATE TABLE paper_ledger (
        entry_id            TEXT PRIMARY KEY,
        signal_id           TEXT REFERENCES signals(signal_id),
        origin              TEXT NOT NULL CHECK (origin IN ('adjudication','manual_operator')),
        thesis              TEXT NOT NULL,
        thesis_hash         TEXT NOT NULL,
        decided_at          TEXT NOT NULL,
        market_id           TEXT,
        condition_id        TEXT,
        token_id            TEXT,
        outcome             TEXT CHECK (outcome IN ('YES','NO')),
        entry_observed_at   TEXT,
        entry_mid           REAL,
        entry_best_bid      REAL,
        entry_best_ask      REAL,
        entry_spread        REAL,
        assumed_size_usd    REAL NOT NULL,
        slippage_rule       TEXT NOT NULL,
        assumed_entry_price REAL,
        expiry_horizon_s    INTEGER NOT NULL,
        expires_at          TEXT NOT NULL,
        markout_horizons_json TEXT NOT NULL,
        invalidations_json  TEXT NOT NULL,
        evidence_refs_json  TEXT NOT NULL,
        -- Two structural guards so a row can never be read as a real fill.
        paper_only          INTEGER NOT NULL DEFAULT 1 CHECK (paper_only = 1),
        fill_type           TEXT NOT NULL DEFAULT 'SIMULATED_NO_FILL'
                            CHECK (fill_type = 'SIMULATED_NO_FILL'),
        created_at          TEXT NOT NULL
      );
      CREATE INDEX idx_ledger_decided ON paper_ledger(decided_at);

      CREATE TABLE paper_ledger_annotations (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        entry_id     TEXT NOT NULL REFERENCES paper_ledger(entry_id),
        recorded_at  TEXT NOT NULL,
        kind         TEXT NOT NULL CHECK (kind IN ('markout','outcome','note','invalidated')),
        note         TEXT,
        detail_json  TEXT
      );
      CREATE INDEX idx_annotations_entry ON paper_ledger_annotations(entry_id, id);
    `,
  },
];

export function applyMigrations(db: Database.Database): number {
  db.exec(`
    CREATE TABLE IF NOT EXISTS schema_migrations (
      version    INTEGER PRIMARY KEY,
      name       TEXT NOT NULL,
      applied_at TEXT NOT NULL
    );
  `);

  const applied = new Set(
    db
      .prepare('SELECT version FROM schema_migrations')
      .all()
      .map((row) => (row as { version: number }).version),
  );

  const record = db.prepare(
    'INSERT INTO schema_migrations (version, name, applied_at) VALUES (?, ?, ?)',
  );

  for (const migration of MIGRATIONS) {
    if (applied.has(migration.version)) continue;
    db.transaction(() => {
      db.exec(migration.up);
      record.run(migration.version, migration.name, new Date().toISOString());
    })();
  }

  const row = db.prepare('SELECT MAX(version) AS v FROM schema_migrations').get() as { v: number };
  return row.v ?? 0;
}
