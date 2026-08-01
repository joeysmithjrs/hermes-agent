import { readFileSync, writeFileSync } from 'node:fs';

import { UsageError } from '../../core/errors.js';
import { resolveHermesHome } from '../../hermes/prompts.js';
import { DEFAULT_GATE_ID, readGateSignal, stampApproval } from '../../plan/approval.js';
import { briefWarning, checkPlanBrief } from '../../plan/brief.js';
import { DEFAULT_PLAN_NODE_ID, DEFAULT_WORKSPACE, planFromRun } from '../../plan/from-run.js';
import { renderPlanSummary, renderPlanTelegram } from '../../plan/render.js';
import {
  executionPlanJsonSchema,
  parseExecutionPlan,
  type ExecutionPlan,
} from '../../schema/execution-plan.js';
import type { Flags } from '../args.js';
import { emit } from '../output.js';

/**
 * `pm-desk plan ...` — everything that reads or checks an ExecutionPlan.
 *
 * The plan is the artifact the morning generator emits and the gate approves,
 * so these commands sit exactly on the seam between the Hermes run and the
 * deterministic provisioner. Every one of them is read-only except `from-run
 * --out` and `approve --out`, which write a single JSON file the operator names.
 */
export async function planCommand(sub: string | undefined, flags: Flags): Promise<number> {
  const json = flags.bool('json');

  switch (sub) {
    case 'validate': {
      const file = flags.required('file', 'Path to an ExecutionPlan JSON file.');
      const strictBrief = flags.bool('strict-brief');
      flags.rejectUnknown('plan validate');
      const plan = readPlan(file);
      const brief = checkPlanBrief(plan);

      // --strict-brief is the reopen posture: a brief missing an edge-first
      // section is a hard failure, not a warning. Without it (the morning
      // posture) the missing sections are surfaced but the plan still validates.
      if (strictBrief && !brief.ok) {
        throw new UsageError(
          `telegram_brief is missing edge-first section(s): ${brief.missing.join(', ')}`,
          {
            hint: 'A reopen brief must carry CLAIM / WHY GAP CAN EXIST / MEASURED / KILLS / IF YOU APPROVE.',
          },
        );
      }

      emit(
        { json },
        {
          valid: true,
          plan_id: plan.plan_id,
          version: plan.version,
          paper_only: plan.paper_only,
          monitors: plan.monitors.length,
          hermes_setup: plan.hermes_setup.length,
          approval: plan.approval.decision,
          brief_ok: brief.ok,
          brief_missing: brief.missing,
          strict_brief: strictBrief,
        },
        () =>
          [
            `OK — ExecutionPlan v${plan.version} ${plan.plan_id}`,
            `  paper_only              ${plan.paper_only}`,
            `  live_execution_allowed  ${plan.paper_only_constraints.live_execution_allowed}`,
            `  monitors                ${plan.monitors.length}`,
            `  hermes_setup            ${plan.hermes_setup.length}`,
            `  approval                ${plan.approval.decision}`,
            brief.ok
              ? '  brief                  edge-first sections present'
              : `  brief                  ⚠ missing: ${brief.missing.join(', ')}`,
          ].join('\n'),
      );
      return 0;
    }

    case 'show': {
      const file = flags.required('file', 'Path to an ExecutionPlan JSON file.');
      flags.rejectUnknown('plan show');
      const plan = readPlan(file);
      emit({ json }, plan, () => renderPlanSummary(plan));
      return 0;
    }

    case 'render-telegram': {
      const file = flags.required('file', 'Path to an ExecutionPlan JSON file.');
      flags.rejectUnknown('plan render-telegram');
      const plan = readPlan(file);
      const text = renderPlanTelegram(plan);
      // Soft warn (stderr) when the brief lacks the edge-first sections, so a
      // rendered brief that is not decision-ready is flagged without polluting
      // the Telegram message Joe approves. Hard enforcement lives in `validate
      // --strict-brief`.
      const warning = briefWarning(plan);
      if (warning) process.stderr.write(`${warning}\n`);
      emit({ json }, { text, chars: text.length, paper_only: true, brief_ok: warning === null }, () => text);
      return 0;
    }

    case 'schema': {
      flags.rejectUnknown('plan schema');
      // Always JSON: the only use for this is piping into a file or a prompt.
      emit({ json: true }, executionPlanJsonSchema(), () => '');
      return 0;
    }

    case 'from-run': {
      const runId = flags.required('run-id', 'A Hermes run id from `hermes workflow list`.');
      const nodeId = flags.str('node', DEFAULT_PLAN_NODE_ID)!;
      const workspace = flags.str('workspace', DEFAULT_WORKSPACE)!;
      const hermesHome = resolveHermesHome(flags.str('hermes-home'));
      const out = flags.str('out');
      flags.rejectUnknown('plan from-run');

      const found = planFromRun({ hermesHome, runId, nodeId, workspace });
      if (out) writeFileSync(out, `${JSON.stringify(found.plan, null, 2)}\n`, 'utf8');

      emit(
        { json },
        {
          plan_id: found.plan.plan_id,
          source_path: found.source_path,
          source_kind: found.source_kind,
          ...(found.node_run_id ? { node_run_id: found.node_run_id } : {}),
          ...(found.node_id ? { node_id: found.node_id } : {}),
          ...(out ? { written_to: out } : {}),
          plan: found.plan,
        },
        () =>
          [
            `found ExecutionPlan ${found.plan.plan_id}`,
            found.source_kind === 'node_output'
              ? `  node       ${found.node_id ?? '(unmapped)'} · node_run ${found.node_run_id}`
              : // Worth saying out loud: the node envelope held no usable plan,
                // so the gate never opened and this came off disk instead.
                '  source     workspace artifact (the plan node emitted no parseable JSON)',
            `  read from  ${found.source_path}`,
            out ? `  written to ${out}` : '  (pass --out <file> to save it)',
          ].join('\n'),
      );
      return 0;
    }

    case 'after-gate': {
      // The bridge between "Joe decided in Telegram" and "what actually
      // installs." Pulls the plan back out of the run, says how many monitors
      // it carries, and prints the exact provision command — so an operator who
      // just tapped approve does not have to remember the dry-run/apply flow,
      // and a plan that installs nothing says so out loud (E3).
      const runId = flags.required('run-id', 'A Hermes run id whose gate Joe decided.');
      const nodeId = flags.str('node', DEFAULT_PLAN_NODE_ID)!;
      const workspace = flags.str('workspace', DEFAULT_WORKSPACE)!;
      const hermesHome = resolveHermesHome(flags.str('hermes-home'));
      const deskHome = flags.str('desk-home') ?? flags.str('home');
      flags.rejectUnknown('plan after-gate');

      const found = planFromRun({ hermesHome, runId, nodeId, workspace });
      const plan = found.plan;
      const monitors = plan.monitors.length;
      const approved = plan.approval.decision === 'approved';
      const planFile = '<run plan> (save with: pm-desk plan from-run --run-id <id> --out plan.json)';

      const nextCommand =
        monitors === 0
          ? 'nothing installs — record only (no_monitors_reason on file).'
          : approved
            ? `pm-desk provision apply --plan plan.json --hermes-home ${hermesHome} --desk-home ${deskHome ?? '<desk-home>'} --i-approved-this-plan`
            : `pm-desk provision dry-run --plan plan.json --hermes-home ${hermesHome} --desk-home ${deskHome ?? '<desk-home>'}`;

      emit(
        { json },
        {
          plan_id: plan.plan_id,
          run_id: runId,
          source_path: found.source_path,
          approval_decision: plan.approval.decision,
          approved,
          monitors,
          hermes_setup: plan.hermes_setup.length,
          next_command: nextCommand,
        },
        () =>
          [
            `after-gate — plan ${plan.plan_id} (run ${runId})`,
            `  read from      ${found.source_path}`,
            `  approval       ${plan.approval.decision}`,
            `  monitors       ${monitors}`,
            `  hermes_setup   ${plan.hermes_setup.length}`,
            '',
            monitors === 0
              ? '  Nothing installs. The plan is recorded; no cron jobs are created.'
              : approved
                ? '  Approved — install with:'
                : '  Not yet approved — dry-run with:',
            `    ${nextCommand}`,
            '',
            `  ${planFile}`,
          ].join('\n'),
      );
      return 0;
    }

    case 'approve': {
      const file = flags.required('file', 'Path to the ExecutionPlan JSON emitted by the run.');
      const runId = flags.required('run-id', 'The Hermes run whose gate Joe decided.');
      const gateId = flags.str('gate', DEFAULT_GATE_ID)!;
      const hermesHome = resolveHermesHome(flags.str('hermes-home'));
      const out = flags.str('out', file)!;
      flags.rejectUnknown('plan approve');

      const plan = readPlan(file);
      const signal = readGateSignal(hermesHome, runId, gateId);
      const stamped = stampApproval({
        plan: { ...plan, ...(plan.generator_run_id ? {} : { generator_run_id: runId }) },
        signal,
      });
      writeFileSync(out, `${JSON.stringify(stamped, null, 2)}\n`, 'utf8');

      emit(
        { json },
        {
          plan_id: stamped.plan_id,
          gate_id: gateId,
          hermes_decision: signal.decision,
          approval: stamped.approval.decision,
          written_to: out,
        },
        () =>
          [
            `gate ${gateId} on run ${runId} recorded: ${signal.decision}`,
            `plan ${stamped.plan_id} approval is now: ${stamped.approval.decision}`,
            `written to ${out}`,
            '',
            stamped.approval.decision === 'approved'
              ? 'Next: pm-desk provision dry-run --plan <file> --hermes-home <dir> --desk-home <dir>'
              : 'The provisioner will refuse to apply this plan. Nothing will be installed.',
          ].join('\n'),
      );
      // A non-approval is a real outcome a script should be able to branch on.
      return stamped.approval.decision === 'approved' ? 0 : 1;
    }

    default:
      throw new UsageError(`unknown \`plan\` subcommand: ${sub ?? '(none)'}`, {
        hint: 'Try: pm-desk plan validate --file <f> [--strict-brief] | plan show | plan render-telegram | plan schema | plan from-run --run-id <id> | plan approve --file <f> --run-id <id> | plan after-gate --run-id <id>',
      });
  }
}

function readPlan(path: string): ExecutionPlan {
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(path, 'utf8'));
  } catch (cause) {
    throw new UsageError(`cannot read an ExecutionPlan JSON from ${path}`, {
      hint: 'Check the path and that the file contains a single JSON object.',
      cause,
    });
  }
  return parseExecutionPlan(raw);
}
