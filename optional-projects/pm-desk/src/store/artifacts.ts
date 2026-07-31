import { existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';

import type Database from 'better-sqlite3';

import { StoreError } from '../core/errors.js';
import { sha256Hex } from '../core/hash.js';
import { nowIso } from '../core/time.js';

export interface ArtifactRecord {
  ref: string;
  hash: string;
  path: string;
  bytes: number;
  media_type: string;
  created_at: string;
}

/**
 * Content-addressed retention for raw and normalized evidence bodies.
 *
 * The ref (`sha256:<hex>`) *is* the integrity proof: a signal that cites an
 * artifact_ref can be re-verified later by rehashing the file. Writing the same
 * bytes twice is a no-op, which is what keeps repeated polling of an unchanged
 * page from growing the store.
 */
export class ArtifactStore {
  constructor(
    private readonly db: Database.Database,
    private readonly root: string,
  ) {}

  private relPathFor(hash: string): string {
    // Two-level fan-out keeps directory sizes sane on a busy desk.
    return join(hash.slice(0, 2), hash.slice(2, 4), `${hash}.bin`);
  }

  put(content: string | Uint8Array, mediaType = 'text/plain'): ArtifactRecord {
    const hash = sha256Hex(content);
    const ref = `sha256:${hash}`;
    const existing = this.find(ref);
    if (existing) return existing;

    const relPath = this.relPathFor(hash);
    const absPath = join(this.root, relPath);
    mkdirSync(dirname(absPath), { recursive: true });
    writeFileSync(absPath, content);
    const bytes = typeof content === 'string' ? Buffer.byteLength(content) : content.byteLength;
    const created_at = nowIso();

    this.db
      .prepare(
        `INSERT OR IGNORE INTO artifacts (hash, rel_path, bytes, media_type, created_at)
         VALUES (?, ?, ?, ?, ?)`,
      )
      .run(hash, relPath, bytes, mediaType, created_at);

    return { ref, hash, path: absPath, bytes, media_type: mediaType, created_at };
  }

  find(ref: string): ArtifactRecord | undefined {
    const hash = stripRef(ref);
    const row = this.db.prepare('SELECT * FROM artifacts WHERE hash = ?').get(hash) as
      | { hash: string; rel_path: string; bytes: number; media_type: string; created_at: string }
      | undefined;
    if (!row) return undefined;
    return {
      ref: `sha256:${row.hash}`,
      hash: row.hash,
      path: join(this.root, row.rel_path),
      bytes: row.bytes,
      media_type: row.media_type,
      created_at: row.created_at,
    };
  }

  read(ref: string): string {
    const record = this.find(ref);
    if (!record) {
      throw new StoreError(`unknown artifact ref: ${ref}`, {
        hint: 'The referenced evidence is not in this store. Re-run the collector, or point PM_DESK_HOME at the store that produced the signal.',
      });
    }
    if (!existsSync(record.path)) {
      throw new StoreError(`artifact ${ref} is indexed but its file is missing`, {
        hint: `Expected it at ${record.path}. The artifact directory may have been pruned.`,
      });
    }
    return readFileSync(record.path, 'utf8');
  }

  /** Re-hash the stored bytes and confirm they still match the ref. */
  verify(ref: string): boolean {
    const record = this.find(ref);
    if (!record || !existsSync(record.path)) return false;
    return sha256Hex(readFileSync(record.path)) === record.hash;
  }

  count(): number {
    return (this.db.prepare('SELECT COUNT(*) AS n FROM artifacts').get() as { n: number }).n;
  }
}

function stripRef(ref: string): string {
  if (!/^sha256:[0-9a-f]{64}$/.test(ref)) {
    throw new StoreError(`malformed artifact ref: ${ref}`, {
      hint: 'Artifact refs look like sha256:<64 lowercase hex chars>.',
    });
  }
  return ref.slice('sha256:'.length);
}
