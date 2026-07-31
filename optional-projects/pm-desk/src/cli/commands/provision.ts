import { readFileSync } from 'node:fs';

import { UsageError } from '../../core/errors.js';
import { packageRoot } from '../../hermes/package-paths.js';
import { resolveHermesHome } from '../../hermes/prompts.js';
import { provision, type ProvisionResult } from '../../provision/provisioner.js';
import { provisionStatus, revokePlan } from '../../provision/revoke.js';
import { parseExecutionPlan, type ExecutionPlan } from '../../schema/execution-plan.js';
import { openStore } from '../../store/index.js';
import type { Flags } from '../args.js';
import { emit, table } from '../output.js';

const ACK_FLAG = 'i-approved-this-plan';

/**
 * `pm-desk provision ...` — installing what an approved plan describes.
 *
 * `dry-run` is the default posture and works on a pending plan, because seeing
 * exactly what would be installed is what an operator needs before deciding.
 * `apply` needs both an approval Hermes recorded and the explicit
 * `--i-approved-this-plan` acknowledgement.
 *
 * `--hermes-home` and `--desk-home` are always the operator's to choose.
 * `--hermes-home` falls back to $HERMES_HOME and then ~/.hermes exactly as
 * Hermes resolves it, and every file written and every cron job created lands
 * in that home — never silently in a different one.
 */
export async function provisionCommand(sub: string | undefined, flags: Flags): Promise<number> {
  const json = flags.bool('json');

  switch (sub) {
    case 'dry-run':
    case 'apply': {
      const planPath = flags.required('plan', 'Path to the ExecutionPlan JSON.');
      const hermesHome = resolveHermesHome(flags.str('hermes-home'));
      const deskHome = flags.str('desk-home') ?? flags.str('home');
      const acknowledged = flags.bool(ACK_FLAG);
      const hermesBin = flags.str('hermes-bin');
      const nodeBin = flags.str('node-bin');
      flags.rejectUnknown(`provision ${sub}`);

      const plan = readPlan(planPath);
      const store = openStore({ ...(deskHome ? { home: deskHome } : {}) });
      try {
        const result = await provision({
          plan,
          store,
          hermesHome,
          deskHome: store.home,
          packageDir: packageRoot(),
          mode: sub === 'apply' ? 'apply' : 'dry-run',
          acknowledged,
          ...(hermesBin ? { hermesBin } : {}),
          ...(nodeBin ? { nodeBin } : {}),
        });
        emit({ json }, result, () => renderProvision(result));
        return result.ok ? 0 : 1;
      } finally {
        store.close();
      }
    }

    case 'status': {
      const planId = flags.required('plan-id', 'A plan_id from a previous dry-run or apply.');
      const deskHome = flags.str('desk-home') ?? flags.str('home');
      flags.rejectUnknown('provision status');

      const store = openStore({ ...(deskHome ? { home: deskHome } : {}), readonly: true });
      try {
        const status = provisionStatus(store, planId);
        emit({ json }, status, () =>
          [
            `plan ${status.plan_id} — ${status.status} (approval: ${status.approval_decision})`,
            `hermes home ${status.hermes_home}`,
            `desk home   ${status.desk_home}`,
            `updated     ${status.updated_at}`,
            '',
            'ACTIONS',
            table(status.actions, ['action', 'status', 'job', 'applied_at']),
            '',
            `ARTIFACTS (${status.artifacts.length})`,
            table(status.artifacts, ['kind', 'sha256', 'path']),
          ].join('\n'),
        );
        return 0;
      } finally {
        store.close();
      }
    }

    case 'revoke': {
      const planId = flags.required('plan-id', 'A plan_id from a previous apply.');
      const deskHome = flags.str('desk-home') ?? flags.str('home');
      const dryRun = flags.bool('dry-run');
      const hermesBin = flags.str('hermes-bin');
      // Accepted and ignored: revoke removes cron jobs recorded against this
      // plan, which carry their own home. Rejecting it would break a call that
      // reasonably mirrors the apply flags.
      flags.str('hermes-home');
      flags.rejectUnknown('provision revoke');

      const store = openStore({ ...(deskHome ? { home: deskHome } : {}) });
      try {
        const result = await revokePlan({
          store,
          planId,
          dryRun,
          ...(hermesBin ? { hermesBin } : {}),
        });
        emit({ json }, result, () =>
          [
            `${result.dry_run ? 'DRY RUN — nothing removed. ' : ''}plan ${result.plan_id}`,
            '',
            table(
              result.jobs.map((j) => ({
                job_ref: j.job_ref,
                status: j.status,
                reason: j.reason ?? '',
              })),
              ['job_ref', 'status', 'reason'],
            ),
            '',
            `${result.retained_artifacts.length} artifact(s) left in place on purpose (they are the audit trail; a removed cron job cannot run them):`,
            ...result.retained_artifacts.map((p) => `  ${p}`),
          ].join('\n'),
        );
        return result.ok ? 0 : 1;
      } finally {
        store.close();
      }
    }

    default:
      throw new UsageError(`unknown \`provision\` subcommand: ${sub ?? '(none)'}`, {
        hint: `Try: pm-desk provision dry-run --plan <f> --hermes-home <dir> --desk-home <dir> | provision apply --${ACK_FLAG} | provision status --plan-id <id> | provision revoke --plan-id <id>`,
      });
  }
}

function renderProvision(result: ProvisionResult): string {
  const lines: string[] = [];
  lines.push(
    result.mode === 'dry-run'
      ? `DRY RUN — nothing written, nothing spawned. plan ${result.plan_id}`
      : `APPLIED — plan ${result.plan_id}`,
  );
  lines.push('PAPER ONLY — no monitor installed here can place an order.');
  lines.push(`hermes home ${result.hermes_home}`);
  lines.push(`desk home   ${result.desk_home}`);
  lines.push('');

  lines.push(`FILES (${result.files.length})`);
  lines.push(
    table(
      result.files.map((f) => ({ status: f.status, kind: f.kind, path: f.path })),
      ['status', 'kind', 'path'],
    ),
  );
  lines.push('');

  lines.push(`COMMANDS (${result.actions.length})`);
  for (const action of result.actions) {
    lines.push(`  [${action.status}] ${action.kind}`);
    lines.push(`    ${action.display}`);
    if (action.cron_job_id) lines.push(`    job id: ${action.cron_job_id}`);
    if (action.reason) lines.push(`    ${action.reason}`);
  }
  if (result.actions.length === 0) lines.push('  (none)');
  lines.push('');

  if (result.declared_only.length > 0) {
    lines.push(`NOT EXECUTED (${result.declared_only.length}) — printed for you to run or ignore`);
    for (const item of result.declared_only) {
      lines.push(`  [${item.action}] ${item.apply_command}`);
      lines.push(`    ${item.reason}`);
    }
    lines.push('');
  }

  if (result.drift.length > 0) {
    // Not fatal, and it does not change what runs — but an approver who read a
    // different command than the one being spawned needs to be told.
    lines.push(`⚠ COMMAND DRIFT (${result.drift.length})`);
    lines.push('  The plan showed a different command than the provisioner derived.');
    lines.push('  The derived one is what runs; the plan text is display only.');
    for (const item of result.drift) {
      lines.push(`  ${item.idempotency_key}`);
      lines.push(`    plan says: ${item.declared}`);
      lines.push(`    will run:  ${item.derived}`);
    }
    lines.push('');
  }

  lines.push(
    result.mode === 'dry-run'
      ? `Next: pm-desk provision apply --plan <file> --hermes-home ${result.hermes_home} --desk-home ${result.desk_home} --${ACK_FLAG}`
      : `Undo with: pm-desk provision revoke --plan-id ${result.plan_id} --desk-home ${result.desk_home}`,
  );
  return lines.join('\n');
}

function readPlan(path: string): ExecutionPlan {
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(path, 'utf8'));
  } catch (cause) {
    throw new UsageError(`cannot read an ExecutionPlan JSON from ${path}`, {
      hint: 'Check the path. `pm-desk plan from-run --run-id <id> --out <file>` writes one.',
      cause,
    });
  }
  return parseExecutionPlan(raw);
}
