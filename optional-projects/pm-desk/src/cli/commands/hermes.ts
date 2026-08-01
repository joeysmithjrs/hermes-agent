import { UsageError } from '../../core/errors.js';
import {
  ADJUDICATION_WORKFLOW_FILE,
  GENERATOR_WORKFLOW_FILE,
  packageWorkflowPath,
  REOPEN_WORKFLOW_FILE,
  RESEARCH_WORKFLOW_FILE,
} from '../../hermes/package-paths.js';
import { applyPromptInstall, planPromptInstall } from '../../hermes/prompts.js';
import type { Flags } from '../args.js';
import { emit, table } from '../output.js';

/**
 * `pm-desk hermes ...` — the small, explicit surface that binds this optional
 * package to a Hermes install. Every subcommand here is read-only or
 * plan-by-default; the only one that writes is `install-prompts --apply`, and
 * it writes the prompt-library files this package ships under
 * `$HERMES_HOME/workflows/prompts/`. Nothing here touches Hermes' config.yaml,
 * enables a webhook, or creates a cron job.
 *
 * Creating cron jobs is `pm-desk provision apply`'s job, and only for a plan
 * Joe approved at a gate.
 */
export async function hermesCommand(sub: string | undefined, flags: Flags): Promise<number> {
  const json = flags.bool('json');

  switch (sub) {
    case 'install-prompts': {
      const hermesHome = flags.str('hermes-home');
      const apply = flags.bool('apply');
      const force = flags.bool('force');
      flags.rejectUnknown('hermes install-prompts');

      const plan = planPromptInstall({ hermesHome, force });

      if (!apply) {
        emit({ json }, { ...plan, applied: false, mode: 'dry-run' }, () =>
          [
            `DRY RUN — nothing written. Target: ${plan.target_dir}`,
            '',
            table(
              plan.entries.map((e) => ({ library: e.name, action: e.action, target: e.target })),
              ['library', 'action', 'target'],
            ),
            '',
            plan.blocked
              ? 'At least one library differs from the packaged version. Re-run with --apply --force to take the packaged copy, or diff them first.'
              : 'Re-run with --apply to install.',
          ].join('\n'),
        );
        // A blocked plan is a real condition an operator must resolve, so it is
        // a non-zero exit even in dry-run: a script can gate on it.
        return plan.blocked ? 1 : 0;
      }

      const written = applyPromptInstall(plan);
      emit(
        { json },
        { ...plan, applied: true, written: written.map((e) => e.name), mode: 'apply' },
        () =>
          [
            `Installed ${written.length} prompt librar${written.length === 1 ? 'y' : 'ies'} into ${plan.target_dir}`,
            '',
            table(
              plan.entries.map((e) => ({ library: e.name, action: e.action })),
              ['library', 'action'],
            ),
            '',
            'Verify with:',
            `  hermes workflow validate ${packageWorkflowPath(GENERATOR_WORKFLOW_FILE)}`,
          ].join('\n'),
      );
      return 0;
    }

    case 'workflows': {
      flags.rejectUnknown('hermes workflows');
      const rows = [
        {
          workflow: 'pm_morning_generator_v0',
          role: 'THE PRODUCT LOOP — morning idea -> ExecutionPlan -> Telegram gate',
          path: packageWorkflowPath(GENERATOR_WORKFLOW_FILE),
        },
        {
          workflow: 'pm_thesis_reopen_v0',
          role: 'reopen a parked thesis after a buildout ships (prior-run context + harness)',
          path: packageWorkflowPath(REOPEN_WORKFLOW_FILE),
        },
        {
          workflow: 'pm_signal_adjudication_v0',
          role: 'optional subroutine: does one fired signal matter? (tools-empty)',
          path: packageWorkflowPath(ADJUDICATION_WORKFLOW_FILE),
        },
        {
          workflow: 'pm_desk_paper_v0',
          role: 'superseded by the generator; kept for reference',
          path: packageWorkflowPath(RESEARCH_WORKFLOW_FILE),
        },
      ];
      emit({ json }, { workflows: rows }, () => table(rows, ['workflow', 'role', 'path']));
      return 0;
    }

    default:
      throw new UsageError(`unknown \`hermes\` subcommand: ${sub ?? '(none)'}`, {
        hint: 'Try: pm-desk hermes install-prompts [--apply] | pm-desk hermes workflows',
      });
  }
}
