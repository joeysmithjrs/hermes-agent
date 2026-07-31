import { UsageError } from '../core/errors.js';
import type { CommandRunner } from '../ingress/dispatcher.js';
import type { DeskStore } from '../store/index.js';
import { defaultRunner } from './provisioner.js';

/**
 * Undoing a provision.
 *
 * Revoke only ever touches cron jobs this desk recorded creating for this exact
 * plan_id. It does not list the operator's jobs and pattern-match on a prefix:
 * a name that merely looks like ours could be somebody else's, and a
 * provisioner that deletes jobs it did not create is worse than one that leaves
 * a stale job behind.
 *
 * Files are deliberately NOT deleted. The plan copy, the specs and the sweep
 * scripts are the audit trail for whatever the monitors already emitted, and a
 * revoked cron job cannot run them anyway. `status` reports where they are so
 * an operator can remove them by hand if they want to.
 */

export interface RevokeOptions {
  store: DeskStore;
  planId: string;
  runner?: CommandRunner;
  hermesBin?: string;
  /** Print what would happen without removing anything. */
  dryRun?: boolean;
}

export interface RevokedJob {
  idempotency_key: string;
  job_ref: string;
  status: 'planned' | 'revoked' | 'failed' | 'not_applied';
  reason?: string;
}

export interface RevokeResult {
  plan_id: string;
  dry_run: boolean;
  jobs: RevokedJob[];
  /** Files left in place on purpose, listed so nothing is a surprise. */
  retained_artifacts: string[];
  ok: boolean;
}

export async function revokePlan(options: RevokeOptions): Promise<RevokeResult> {
  const { store, planId } = options;
  const record = store.provision.getPlan(planId);
  if (!record) {
    throw new UsageError(`no provisioned plan ${planId} in this desk store`, {
      hint: 'List what this desk knows about with `pm-desk provision status --plan-id <id>`, or check --desk-home.',
    });
  }

  const runner = options.runner ?? defaultRunner;
  const hermesBin = options.hermesBin ?? 'hermes';
  const dryRun = options.dryRun ?? false;

  const jobs: RevokedJob[] = [];
  let ok = true;

  for (const action of store.provision.listActions(planId)) {
    if (action.action !== 'cron_create') continue;
    // `hermes cron remove` takes a job id, and resolves a name too. Prefer the
    // id we parsed at creation time; fall back to the name we chose.
    const jobRef = action.cron_job_id ?? action.cron_job_name;
    if (!jobRef) continue;

    if (action.status !== 'applied') {
      jobs.push({
        idempotency_key: action.idempotency_key,
        job_ref: jobRef,
        status: 'not_applied',
        reason: `recorded as '${action.status}' — nothing to remove`,
      });
      continue;
    }

    if (dryRun) {
      jobs.push({ idempotency_key: action.idempotency_key, job_ref: jobRef, status: 'planned' });
      continue;
    }

    const result = await runner(hermesBin, ['cron', 'remove', jobRef]);
    if (result.code === 0) {
      store.provision.recordAction({
        plan_id: planId,
        idempotency_key: action.idempotency_key,
        action: action.action,
        argv: action.argv,
        status: 'revoked',
        ...(action.cron_job_name ? { cron_job_name: action.cron_job_name } : {}),
        ...(action.cron_job_id ? { cron_job_id: action.cron_job_id } : {}),
        detail: { revoked_via: [hermesBin, 'cron', 'remove', jobRef] },
      });
      jobs.push({ idempotency_key: action.idempotency_key, job_ref: jobRef, status: 'revoked' });
    } else {
      ok = false;
      jobs.push({
        idempotency_key: action.idempotency_key,
        job_ref: jobRef,
        status: 'failed',
        reason: `exited ${result.code}: ${(result.stderr || result.stdout).trim().slice(0, 300) || '(no output)'}`,
      });
    }
  }

  if (!dryRun && ok) store.provision.setPlanStatus(planId, 'revoked');

  return {
    plan_id: planId,
    dry_run: dryRun,
    jobs,
    retained_artifacts: store.provision.listArtifacts(planId).map((a) => a.path),
    ok,
  };
}

export interface ProvisionStatusResult {
  plan_id: string;
  status: string;
  approval_decision: string;
  hermes_home: string;
  desk_home: string;
  created_at: string;
  updated_at: string;
  actions: {
    idempotency_key: string;
    action: string;
    status: string;
    job: string;
    applied_at: string;
  }[];
  artifacts: { path: string; kind: string; sha256: string }[];
}

export function provisionStatus(store: DeskStore, planId: string): ProvisionStatusResult {
  const record = store.provision.getPlan(planId);
  if (!record) {
    throw new UsageError(`no provisioned plan ${planId} in this desk store`, {
      hint: 'A plan appears here after `pm-desk provision dry-run` or `apply`. Check --desk-home.',
    });
  }
  return {
    plan_id: record.plan_id,
    status: record.status,
    approval_decision: record.approval_decision,
    hermes_home: record.hermes_home,
    desk_home: record.desk_home,
    created_at: record.created_at,
    updated_at: record.updated_at,
    actions: store.provision.listActions(planId).map((a) => ({
      idempotency_key: a.idempotency_key,
      action: a.action,
      status: a.status,
      job: a.cron_job_id ?? a.cron_job_name ?? '',
      applied_at: a.applied_at ?? '',
    })),
    artifacts: store.provision
      .listArtifacts(planId)
      .map((a) => ({ path: a.path, kind: a.kind, sha256: a.sha256.slice(0, 12) })),
  };
}
