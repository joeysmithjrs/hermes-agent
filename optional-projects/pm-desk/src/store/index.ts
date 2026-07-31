import { existsSync, mkdirSync } from 'node:fs';
import { dirname, isAbsolute, join, resolve } from 'node:path';

import Database from 'better-sqlite3';

import { StoreError } from '../core/errors.js';
import { ArtifactStore } from './artifacts.js';
import { applyMigrations } from './migrations.js';
import { PaperLedgerRepository } from '../ledger/repository.js';
import {
  AdjudicationRepository,
  MarketRepository,
  MonitorStateRepository,
  ObservationRepository,
  SignalRepository,
  SourceRepository,
} from './repositories.js';

export * from './types.js';
export { ArtifactStore } from './artifacts.js';
export type { SignalRecordResult } from './repositories.js';

export interface OpenStoreOptions {
  /** Desk home directory. Defaults to `$PM_DESK_HOME` or `./data`. */
  home?: string;
  /** Override the DB filename inside `home`. */
  dbFile?: string;
  readonly?: boolean;
}

export interface DeskStore {
  readonly home: string;
  readonly dbPath: string;
  readonly artifactsRoot: string;
  readonly db: Database.Database;
  readonly markets: MarketRepository;
  readonly observations: ObservationRepository;
  readonly artifacts: ArtifactStore;
  readonly sources: SourceRepository;
  readonly signals: SignalRepository;
  readonly monitorState: MonitorStateRepository;
  readonly adjudications: AdjudicationRepository;
  readonly ledger: PaperLedgerRepository;
  schemaVersion(): number;
  transaction<T>(fn: () => T): T;
  close(): void;
}

export function resolveHome(home?: string): string {
  const raw = home ?? process.env.PM_DESK_HOME ?? './data';
  return isAbsolute(raw) ? raw : resolve(process.cwd(), raw);
}

/**
 * Opens (creating if needed) the local desk store. Migrations run on every open
 * and are idempotent, so `pm-desk store init` and any CLI entry point converge on
 * the same schema without a separate bootstrap step.
 */
export function openStore(options: OpenStoreOptions = {}): DeskStore {
  const home = resolveHome(options.home);
  const dbPath = join(home, options.dbFile ?? 'pm-desk.sqlite');
  const artifactsRoot = join(home, 'artifacts');

  try {
    mkdirSync(dirname(dbPath), { recursive: true });
    mkdirSync(artifactsRoot, { recursive: true });
  } catch (cause) {
    throw new StoreError(`cannot create desk home at ${home}`, {
      hint: 'Check the path is writable, or set PM_DESK_HOME to a directory you own.',
      cause,
    });
  }

  const readonly = options.readonly ?? false;

  // SQLite cannot open a non-existent file read-only. Rather than making every
  // read-only command fail on a fresh desk, create and migrate it once first —
  // so `taxonomy compile` or `ledger list` works before anything else has run.
  if (readonly && !existsSync(dbPath)) {
    const bootstrap = new Database(dbPath);
    try {
      applyMigrations(bootstrap);
    } finally {
      bootstrap.close();
    }
  }

  let db: Database.Database;
  try {
    db = new Database(dbPath, { readonly });
  } catch (cause) {
    throw new StoreError(`cannot open the desk database at ${dbPath}`, {
      hint: 'Another process may hold it, or the file may be corrupt. Try `pm-desk store status`.',
      cause,
    });
  }

  db.pragma('foreign_keys = ON');
  if (!readonly) {
    // journal_mode and synchronous are writes, so they are skipped on a
    // read-only handle (the mode is already persisted in the file anyway).
    db.pragma('journal_mode = WAL');
    db.pragma('synchronous = NORMAL');
  }

  const version = readonly ? currentVersion(db) : applyMigrations(db);
  const artifacts = new ArtifactStore(db, artifactsRoot);

  return {
    home,
    dbPath,
    artifactsRoot,
    db,
    markets: new MarketRepository(db),
    observations: new ObservationRepository(db),
    artifacts,
    sources: new SourceRepository(db, artifacts),
    signals: new SignalRepository(db),
    monitorState: new MonitorStateRepository(db),
    adjudications: new AdjudicationRepository(db),
    ledger: new PaperLedgerRepository(db),
    schemaVersion: () => version,
    transaction: <T>(fn: () => T): T => db.transaction(fn)(),
    close: () => db.close(),
  };
}

function currentVersion(db: Database.Database): number {
  try {
    const row = db.prepare('SELECT MAX(version) AS v FROM schema_migrations').get() as {
      v: number | null;
    };
    return row.v ?? 0;
  } catch {
    return 0;
  }
}
