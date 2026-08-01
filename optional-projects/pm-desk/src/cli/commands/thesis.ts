/**
 * `pm-desk thesis ...` — the productized thesis-reopen path.
 *
 * A morning thesis is often parked because the quantitative step that would
 * decide it did not exist yet. Once that buildout ships (e.g. the CPI nowcast
 * harness), the desk should loop back and re-decide the thesis in the SAME
 * workspace with prior context — not run a whole new morning lottery. This
 * command packs the prior-run pointer + focus into a workspace file the
 * `pm-thesis-reopen-v0` workflow reads as its input, and prints the run command
 * so the operator does not have to remember the YAML path or the input wiring.
 *
 * Paper only. Nothing here trades, signs, or touches a wallet.
 */

import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';

import { UsageError } from '../../core/errors.js';
import { REOPEN_WORKFLOW_FILE, packageWorkflowPath } from '../../hermes/package-paths.js';
import { resolveHermesHome } from '../../hermes/prompts.js';
import { DEFAULT_WORKSPACE } from '../../plan/from-run.js';
import type { Flags } from '../args.js';
import { emit } from '../output.js';

const REOPEN_HELP = `pm-desk thesis reopen — pack a prior-run pointer + focus for the reopen workflow

Writes <workspace>/thesis_reopen_active.json (the input the
pm-thesis-reopen-v0 workflow reads) and prints the run command.

USAGE
  pm-desk thesis reopen \\
    --prior-run <run_id> --focus-token <token_id> \\
    [--focus-market <id>] [--question <q>] [--bucket 3.4] [--mid 0.43] \\
    [--what-changed <note>] [--json]

FLAGS
  --prior-run <id>      The Hermes run id of the parked morning (required).
  --focus-token <id>    The Polymarket token id to re-snapshot (required).
  --focus-market <id>   The market id, when known.
  --question <q>        The market question, for the brief.
  --bucket <n>          The one-decimal contract bucket to score (e.g. 3.4).
  --mid <n>             A live mid to seed the harness, if already known.
  --what-changed <note> Why the thesis is worth reopening (e.g. "harness shipped").
  --hermes-home <dir>   Hermes home (default $HERMES_HOME then ~/.hermes).
  --json                Machine-readable output.

PAPER ONLY. This command writes one workspace JSON file and prints a command;
it does not run the workflow or trade anything.
`;

/** The JSON written to <workspace>/thesis_reopen_active.json. */
export interface ThesisReopenPack {
  paper_only: true;
  prior_run_id: string;
  workspace: string;
  focus: {
    token_id: string;
    market_id?: string;
    question?: string;
    bucket?: number;
    mid?: number;
  };
  what_changed: string[];
  shipped_tools: string[];
  banned_buildouts: string[];
  workflow_file: string;
}

export async function thesisCommand(sub: string | undefined, flags: Flags): Promise<number> {
  switch (sub) {
    case 'reopen':
      return thesisReopen(flags);
    default:
      throw new UsageError(`unknown \`thesis\` subcommand: ${sub ?? '(none)'}`, {
        hint: 'Try: pm-desk thesis reopen --prior-run <id> --focus-token <id> --json',
      });
  }
}

async function thesisReopen(flags: Flags): Promise<number> {
  if (flags.bool('help')) {
    process.stdout.write(`${REOPEN_HELP}\n`);
    return 0;
  }

  const json = flags.bool('json');
  const priorRun = flags.required('prior-run', 'The Hermes run id of the parked morning.');
  const focusToken = flags.required('focus-token', 'The Polymarket token id to re-snapshot.');
  const focusMarket = flags.str('focus-market');
  const question = flags.str('question');
  const bucketRaw = flags.str('bucket');
  const midRaw = flags.str('mid');
  const whatChanged = flags.list('what-changed');
  const hermesHome = resolveHermesHome(flags.str('hermes-home'));
  flags.rejectUnknown('thesis reopen');

  let bucket: number | undefined;
  if (bucketRaw !== undefined) {
    bucket = Number(bucketRaw);
    if (!Number.isFinite(bucket)) {
      throw new UsageError(`--bucket must be a number, got ${JSON.stringify(bucketRaw)}`, {
        hint: 'Example: --bucket 3.4',
      });
    }
  }
  let mid: number | undefined;
  if (midRaw !== undefined) {
    mid = Number(midRaw);
    if (!Number.isFinite(mid) || mid < 0 || mid > 1) {
      throw new UsageError('--mid must be a probability in [0,1]', { hint: 'Example: --mid 0.43' });
    }
  }

  const workspaceDir = join(hermesHome, 'workflows', 'workspaces', DEFAULT_WORKSPACE);
  const workflowPath = packageWorkflowPath(REOPEN_WORKFLOW_FILE);

  const pack: ThesisReopenPack = {
    paper_only: true,
    prior_run_id: priorRun,
    workspace: DEFAULT_WORKSPACE,
    focus: {
      token_id: focusToken,
      ...(focusMarket ? { market_id: focusMarket } : {}),
      ...(question ? { question } : {}),
      ...(bucket !== undefined ? { bucket } : {}),
      ...(mid !== undefined ? { mid } : {}),
    },
    what_changed: whatChanged.length > 0 ? whatChanged : ['harness shipped: pm-desk research cpi-calibrate'],
    shipped_tools: ['cpi_nowcast_bucket_harness -> pm-desk research cpi-calibrate'],
    banned_buildouts: ['cpi_nowcast_bucket_harness'],
    workflow_file: REOPEN_WORKFLOW_FILE,
  };

  mkdirSync(workspaceDir, { recursive: true });
  const packPath = join(workspaceDir, 'thesis_reopen_active.json');
  writeFileSync(packPath, `${JSON.stringify(pack, null, 2)}\n`, 'utf8');

  const runCommand =
    `hermes workflow run ${workflowPath} \\\n  --input "$(cat ${packPath})"`;

  emit(
    { json },
    {
      paper_only: true,
      prior_run_id: priorRun,
      workspace: DEFAULT_WORKSPACE,
      pack_path: packPath,
      workflow_file: REOPEN_WORKFLOW_FILE,
      run_command: runCommand,
    },
    () =>
      [
        '[PAPER ONLY] thesis reopen packed.',
        `  prior run   ${priorRun}`,
        `  focus token ${focusToken}`,
        `  pack        ${packPath}`,
        '',
        'Run the reopen workflow with:',
        `  ${runCommand}`,
        '',
        'Resume a parked run at its gate with:',
        `  hermes workflow run ${workflowPath} --resume <run_id>`,
      ].join('\n'),
  );
  return 0;
}
