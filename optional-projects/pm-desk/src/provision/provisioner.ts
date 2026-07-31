import { chmodSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname } from 'node:path';

import { ConfigError, UsageError } from '../core/errors.js';
import { sha256Hex } from '../core/hash.js';
import { applyPromptInstall, planPromptInstall } from '../hermes/prompts.js';
import type { CommandRunner } from '../ingress/dispatcher.js';
import type { ExecutionPlan } from '../schema/execution-plan.js';
import type { DeskStore } from '../store/index.js';
import { deriveProvision, type DerivedAction, type DerivedProvision } from './derive.js';

/**
 * The provisioner: the deterministic half of the loop.
 *
 * Everything it can do is decided by `deriveProvision`, which is pure. This
 * module only executes that derivation — writes the files, spawns the argv,
 * records what happened. There is no agent here and no branch that consults
 * one.
 *
 * Three refusals are load-bearing and are checked before anything is written:
 *
 *   1. `paper_only` must be true and `live_execution_allowed` false. The schema
 *      already makes the alternative unrepresentable; this is the second lock.
 *   2. `apply` requires `approval.decision === 'approved'`, which only
 *      `pm-desk plan approve` can set, and only from Hermes' own gate record.
 *   3. `apply` requires the explicit `--i-approved-this-plan` acknowledgement,
 *      so a plan file that happens to say "approved" cannot be installed by a
 *      script that forgot which mode it was in.
 *
 * `dry-run` is exempt from (2) and (3) on purpose: previewing a pending plan is
 * exactly what an operator wants to do before deciding.
 */

export type ProvisionMode = 'dry-run' | 'apply';

export interface ProvisionOptions {
  plan: ExecutionPlan;
  store: DeskStore;
  hermesHome: string;
  deskHome: string;
  packageDir: string;
  mode: ProvisionMode;
  /** Required for `apply`. Nothing is spawned or written without it. */
  acknowledged?: boolean;
  runner?: CommandRunner;
  nodeBin?: string;
  hermesBin?: string;
}

export interface ActionOutcome {
  idempotency_key: string;
  kind: string;
  argv: string[];
  display: string;
  status: 'planned' | 'applied' | 'skipped' | 'failed';
  reason?: string;
  cron_job_name?: string;
  cron_job_id?: string;
}

export interface FileOutcome {
  path: string;
  kind: string;
  status: 'planned' | 'written' | 'unchanged';
  sha256: string;
}

export interface ProvisionResult {
  plan_id: string;
  mode: ProvisionMode;
  paper_only: true;
  hermes_home: string;
  desk_home: string;
  files: FileOutcome[];
  actions: ActionOutcome[];
  declared_only: DerivedProvision['declared_only'];
  drift: DerivedProvision['drift'];
  ok: boolean;
}

export async function provision(options: ProvisionOptions): Promise<ProvisionResult> {
  const { plan, store, mode } = options;

  assertPaperOnly(plan);
  if (mode === 'apply') assertApplyAllowed(plan, options.acknowledged ?? false);

  const derived = deriveProvision({
    plan,
    hermesHome: options.hermesHome,
    deskHome: options.deskHome,
    packageDir: options.packageDir,
    ...(options.nodeBin ? { nodeBin: options.nodeBin } : {}),
    ...(options.hermesBin ? { hermesBin: options.hermesBin } : {}),
  });

  store.provision.upsertPlan({
    plan,
    plan_hash: derived.plan_hash,
    status: mode === 'apply' ? 'applied' : 'dry_run',
    hermes_home: options.hermesHome,
    desk_home: options.deskHome,
  });

  const files: FileOutcome[] = [];
  for (const file of derived.files) {
    const sha = sha256Hex(file.contents);
    if (mode === 'dry-run') {
      files.push({ path: file.path, kind: file.kind, status: 'planned', sha256: sha });
      continue;
    }
    const status = writeFile(file.path, file.contents, file.mode);
    store.provision.recordArtifact({
      plan_id: plan.plan_id,
      path: file.path,
      sha256: sha,
      kind: file.kind,
    });
    files.push({ path: file.path, kind: file.kind, status, sha256: sha });
  }

  const actions: ActionOutcome[] = [];
  let ok = true;
  for (const action of derived.actions) {
    const outcome = await runAction(action, options, derived);
    actions.push(outcome);
    if (outcome.status === 'failed') ok = false;
    store.provision.recordAction({
      plan_id: plan.plan_id,
      idempotency_key: action.idempotency_key,
      action: action.kind,
      argv: action.argv,
      status: outcome.status === 'planned' ? 'planned' : outcome.status,
      ...(outcome.cron_job_name ? { cron_job_name: outcome.cron_job_name } : {}),
      ...(outcome.cron_job_id ? { cron_job_id: outcome.cron_job_id } : {}),
      ...(outcome.reason ? { detail: { reason: outcome.reason } } : {}),
    });
  }

  for (const item of derived.declared_only) {
    store.provision.recordAction({
      plan_id: plan.plan_id,
      idempotency_key: item.idempotency_key,
      action: item.action,
      argv: [],
      status: 'recipe_only',
      detail: { reason: item.reason, apply_command: item.apply_command },
    });
  }

  if (mode === 'apply' && !ok) store.provision.setPlanStatus(plan.plan_id, 'failed');

  return {
    plan_id: plan.plan_id,
    mode,
    paper_only: true,
    hermes_home: options.hermesHome,
    desk_home: options.deskHome,
    files,
    actions,
    declared_only: derived.declared_only,
    drift: derived.drift,
    ok,
  };
}

async function runAction(
  action: DerivedAction,
  options: ProvisionOptions,
  derived: DerivedProvision,
): Promise<ActionOutcome> {
  const base = {
    idempotency_key: action.idempotency_key,
    kind: action.kind,
    argv: action.argv,
    display: action.display,
    ...(action.cron_job_name ? { cron_job_name: action.cron_job_name } : {}),
  };

  if (options.mode === 'dry-run') {
    return { ...base, status: 'planned' };
  }

  // Idempotency: an action that already succeeded for this plan is not redone.
  // Without this, a second `apply` would create a second identical cron job.
  const previous = options.store.provision.getAction(derived.plan_id, action.idempotency_key);
  if (previous?.status === 'applied') {
    return {
      ...base,
      status: 'skipped',
      reason: `already applied${previous.applied_at ? ` at ${previous.applied_at}` : ''}`,
      ...(previous.cron_job_id ? { cron_job_id: previous.cron_job_id } : {}),
    };
  }

  if (action.kind === 'install_prompts') {
    // In-process rather than shelling out to ourselves: this is our own code,
    // and it already refuses to clobber a hand-edited library.
    const installPlan = planPromptInstall({ hermesHome: options.hermesHome });
    if (installPlan.blocked) {
      const names = installPlan.entries.filter((e) => e.action === 'conflict').map((e) => e.name);
      return {
        ...base,
        status: 'failed',
        reason: `prompt libraries differ from the packaged versions and were not overwritten: ${names.join(', ')}`,
      };
    }
    const written = applyPromptInstall(installPlan);
    return { ...base, status: 'applied', reason: `${written.length} librar(y|ies) written` };
  }

  const runner = options.runner ?? defaultRunner;
  const [command, ...args] = action.argv;
  if (!command) return { ...base, status: 'failed', reason: 'derived an empty argv' };

  const result = await runner(command, args);
  if (result.code !== 0) {
    return {
      ...base,
      status: 'failed',
      reason: `exited ${result.code}: ${(result.stderr || result.stdout).trim().slice(0, 500) || '(no output)'}`,
    };
  }

  return {
    ...base,
    status: 'applied',
    ...(action.kind === 'cron_create'
      ? { ...pick(parseCronJobId(result.stdout), 'cron_job_id') }
      : {}),
  };
}

function pick(value: string | undefined, key: 'cron_job_id'): Record<string, string> {
  return value === undefined ? {} : { [key]: value };
}

/**
 * `hermes cron create` prints `Created job: <id>` (hermes_cli/cron.py). The line
 * is colourised, so ANSI escapes are stripped before matching. A miss is not an
 * error — the job name is recorded either way, and `hermes cron remove` resolves
 * a name as well as an id.
 */
export function parseCronJobId(stdout: string): string | undefined {
  // eslint-disable-next-line no-control-regex -- stripping real ANSI escapes
  const plain = stdout.replace(/\[[0-9;]*m/g, '');
  return /Created job:\s*(\S+)/.exec(plain)?.[1];
}

function writeFile(path: string, contents: string, mode: number): 'written' | 'unchanged' {
  let existing: string | undefined;
  try {
    existing = readFileSync(path, 'utf8');
  } catch {
    existing = undefined;
  }
  if (existing === contents) return 'unchanged';
  mkdirSync(dirname(path), { recursive: true });
  writeFileSync(path, contents, 'utf8');
  chmodSync(path, mode);
  return 'written';
}

function assertPaperOnly(plan: ExecutionPlan): void {
  // Both are zod literals, so reaching here means the object bypassed the
  // parser. Belt and braces: this is the check that must never be removable.
  if (plan.paper_only !== true || plan.paper_only_constraints.live_execution_allowed !== false) {
    throw new ConfigError('refusing to provision a plan that is not paper-only', {
      hint: 'paper_only must be true and paper_only_constraints.live_execution_allowed must be false. This desk has no live-execution path.',
    });
  }
}

function assertApplyAllowed(plan: ExecutionPlan, acknowledged: boolean): void {
  if (plan.approval.decision !== 'approved') {
    throw new UsageError(`refusing to apply a plan whose approval is '${plan.approval.decision}'`, {
      hint: 'Only `pm-desk plan approve --file <f> --run-id <id>` sets this, and only from the decision Hermes recorded when the Telegram gate was answered. Use `provision dry-run` to preview a pending plan.',
    });
  }
  if (!acknowledged) {
    throw new UsageError('refusing to apply without --i-approved-this-plan', {
      hint: 'Applying creates cron jobs and writes into your Hermes home. Pass --i-approved-this-plan to say you read the plan, or use `provision dry-run`.',
    });
  }
}

const defaultRunner: CommandRunner = async (command, args) => {
  const { execFile } = await import('node:child_process');
  return new Promise((resolve) => {
    // execFile, not exec: argv is an array, never a shell string, so nothing in
    // a plan can inject a second command through a quote.
    execFile(command, args, { timeout: 120_000 }, (error, stdout, stderr) => {
      resolve({
        code: error && typeof error.code === 'number' ? error.code : error ? 1 : 0,
        stdout: String(stdout),
        stderr: String(stderr),
      });
    });
  });
};

export { defaultRunner };
