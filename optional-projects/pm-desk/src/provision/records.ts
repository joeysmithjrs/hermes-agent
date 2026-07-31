import type Database from 'better-sqlite3';

import { nowIso } from '../core/time.js';
import type { ExecutionPlan } from '../schema/execution-plan.js';

/**
 * The provisioner's memory. Without it, `revoke` would have to guess which cron
 * jobs belonged to a plan (and could delete somebody else's), and a second
 * `apply` would create a duplicate job every time.
 *
 * `UNIQUE (plan_id, idempotency_key)` in migration 2 is what makes idempotency
 * a property of the schema rather than of the code remembering to check.
 */

export type ProvisionStatus = 'dry_run' | 'applied' | 'revoked' | 'failed';
export type ActionStatus = 'planned' | 'applied' | 'skipped' | 'failed' | 'revoked' | 'recipe_only';

export interface ProvisionPlanRow {
  plan_id: string;
  plan: ExecutionPlan;
  plan_hash: string;
  status: ProvisionStatus;
  approval_decision: string;
  hermes_home: string;
  desk_home: string;
  created_at: string;
  updated_at: string;
}

export interface ProvisionActionRow {
  id: number;
  plan_id: string;
  idempotency_key: string;
  action: string;
  argv: string[];
  status: ActionStatus;
  cron_job_name?: string;
  cron_job_id?: string;
  detail?: unknown;
  applied_at?: string;
}

export interface ProvisionArtifactRow {
  path: string;
  sha256: string;
  kind: string;
  written_at: string;
}

export class ProvisionRepository {
  constructor(private readonly db: Database.Database) {}

  upsertPlan(input: {
    plan: ExecutionPlan;
    plan_hash: string;
    status: ProvisionStatus;
    hermes_home: string;
    desk_home: string;
  }): void {
    const now = nowIso();
    this.db
      .prepare(
        `INSERT INTO provision_plans
           (plan_id, plan_json, plan_hash, status, approval_decision, hermes_home, desk_home, created_at, updated_at)
         VALUES (@plan_id, @plan_json, @plan_hash, @status, @approval_decision, @hermes_home, @desk_home, @now, @now)
         ON CONFLICT(plan_id) DO UPDATE SET
           plan_json         = excluded.plan_json,
           plan_hash         = excluded.plan_hash,
           status            = excluded.status,
           approval_decision = excluded.approval_decision,
           hermes_home       = excluded.hermes_home,
           desk_home         = excluded.desk_home,
           updated_at        = excluded.updated_at`,
      )
      .run({
        plan_id: input.plan.plan_id,
        plan_json: JSON.stringify(input.plan),
        plan_hash: input.plan_hash,
        status: input.status,
        approval_decision: input.plan.approval.decision,
        hermes_home: input.hermes_home,
        desk_home: input.desk_home,
        now,
      });
  }

  setPlanStatus(planId: string, status: ProvisionStatus): void {
    this.db
      .prepare('UPDATE provision_plans SET status = ?, updated_at = ? WHERE plan_id = ?')
      .run(status, nowIso(), planId);
  }

  getPlan(planId: string): ProvisionPlanRow | undefined {
    const row = this.db.prepare('SELECT * FROM provision_plans WHERE plan_id = ?').get(planId) as
      Record<string, unknown> | undefined;
    if (!row) return undefined;
    return {
      plan_id: String(row['plan_id']),
      plan: JSON.parse(String(row['plan_json'])) as ExecutionPlan,
      plan_hash: String(row['plan_hash']),
      status: String(row['status']) as ProvisionStatus,
      approval_decision: String(row['approval_decision']),
      hermes_home: String(row['hermes_home']),
      desk_home: String(row['desk_home']),
      created_at: String(row['created_at']),
      updated_at: String(row['updated_at']),
    };
  }

  listPlans(limit = 50): ProvisionPlanRow[] {
    const rows = this.db
      .prepare('SELECT plan_id FROM provision_plans ORDER BY created_at DESC LIMIT ?')
      .all(limit) as { plan_id: string }[];
    return rows.map((r) => this.getPlan(r.plan_id)!).filter(Boolean);
  }

  /**
   * Record an action, leaving an already-`applied` row alone.
   *
   * This is the idempotency point: re-running `provision apply` reaches here
   * with the same (plan_id, idempotency_key) and the ON CONFLICT clause refuses
   * to downgrade a row that already succeeded, so the caller can see it was
   * applied before and skip spawning the command.
   */
  recordAction(input: {
    plan_id: string;
    idempotency_key: string;
    action: string;
    argv: string[];
    status: ActionStatus;
    cron_job_name?: string;
    cron_job_id?: string;
    detail?: unknown;
  }): void {
    this.db
      .prepare(
        `INSERT INTO provision_actions
           (plan_id, idempotency_key, action, argv_json, status, cron_job_name, cron_job_id, detail_json, applied_at)
         VALUES (@plan_id, @idempotency_key, @action, @argv_json, @status, @cron_job_name, @cron_job_id, @detail_json, @applied_at)
         ON CONFLICT(plan_id, idempotency_key) DO UPDATE SET
           action        = excluded.action,
           argv_json     = excluded.argv_json,
           status        = excluded.status,
           cron_job_name = COALESCE(excluded.cron_job_name, provision_actions.cron_job_name),
           cron_job_id   = COALESCE(excluded.cron_job_id, provision_actions.cron_job_id),
           detail_json   = excluded.detail_json,
           applied_at    = COALESCE(excluded.applied_at, provision_actions.applied_at)`,
      )
      .run({
        plan_id: input.plan_id,
        idempotency_key: input.idempotency_key,
        action: input.action,
        argv_json: JSON.stringify(input.argv),
        status: input.status,
        cron_job_name: input.cron_job_name ?? null,
        cron_job_id: input.cron_job_id ?? null,
        detail_json: input.detail === undefined ? null : JSON.stringify(input.detail),
        applied_at: input.status === 'applied' ? nowIso() : null,
      });
  }

  getAction(planId: string, key: string): ProvisionActionRow | undefined {
    const row = this.db
      .prepare('SELECT * FROM provision_actions WHERE plan_id = ? AND idempotency_key = ?')
      .get(planId, key) as Record<string, unknown> | undefined;
    return row ? toActionRow(row) : undefined;
  }

  listActions(planId: string): ProvisionActionRow[] {
    const rows = this.db
      .prepare('SELECT * FROM provision_actions WHERE plan_id = ? ORDER BY id')
      .all(planId) as Record<string, unknown>[];
    return rows.map(toActionRow);
  }

  recordArtifact(input: { plan_id: string; path: string; sha256: string; kind: string }): void {
    this.db
      .prepare(
        `INSERT INTO provision_artifacts (plan_id, path, sha256, kind, written_at)
         VALUES (?, ?, ?, ?, ?)
         ON CONFLICT(plan_id, path) DO UPDATE SET
           sha256 = excluded.sha256, kind = excluded.kind, written_at = excluded.written_at`,
      )
      .run(input.plan_id, input.path, input.sha256, input.kind, nowIso());
  }

  listArtifacts(planId: string): ProvisionArtifactRow[] {
    const rows = this.db
      .prepare(
        'SELECT path, sha256, kind, written_at FROM provision_artifacts WHERE plan_id = ? ORDER BY path',
      )
      .all(planId) as Record<string, unknown>[];
    return rows.map((r) => ({
      path: String(r['path']),
      sha256: String(r['sha256']),
      kind: String(r['kind']),
      written_at: String(r['written_at']),
    }));
  }
}

function toActionRow(row: Record<string, unknown>): ProvisionActionRow {
  return {
    id: Number(row['id']),
    plan_id: String(row['plan_id']),
    idempotency_key: String(row['idempotency_key']),
    action: String(row['action']),
    argv: JSON.parse(String(row['argv_json'])) as string[],
    status: String(row['status']) as ActionStatus,
    ...(row['cron_job_name'] ? { cron_job_name: String(row['cron_job_name']) } : {}),
    ...(row['cron_job_id'] ? { cron_job_id: String(row['cron_job_id']) } : {}),
    ...(row['detail_json'] ? { detail: JSON.parse(String(row['detail_json'])) as unknown } : {}),
    ...(row['applied_at'] ? { applied_at: String(row['applied_at']) } : {}),
  };
}
