import { existsSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

import { UsageError } from '../core/errors.js';
import { nowIso } from '../core/time.js';
import { parseExecutionPlan, type ExecutionPlan } from '../schema/execution-plan.js';
import { runDir } from './from-run.js';

/**
 * Stamping a plan `approved` from Hermes' own recorded gate decision.
 *
 * The alternative — a `pm-desk plan approve --yes` that takes the operator's
 * word — would make the whole dual-control story a comment. Here the only thing
 * that can flip a plan to `approved` is a decision Hermes already wrote to disk
 * when Joe answered the Telegram gate:
 *
 *   $HERMES_HOME/workflows/runs/<run_id>/gate_signals/<gate_id>.json
 *   {"gate_id": "paper_gate", "decision": "approve", "note": ..., "status": "decided"}
 *
 * (Shape verified on this host against `workflow/__init__.py:decide_gate`.)
 *
 * pm-desk reads that file and never writes it. If Joe shelved, the plan records
 * `shelved` and the provisioner refuses to apply it.
 */

export type GateDecision = 'approve' | 'shelve' | 'modify';

export interface GateSignal {
  gate_id: string;
  status: string;
  decision?: GateDecision;
  note?: string;
}

export const DEFAULT_GATE_ID = 'paper_gate';

export function readGateSignal(hermesHome: string, runId: string, gateId: string): GateSignal {
  const path = join(runDir(hermesHome, runId), 'gate_signals', `${gateId}.json`);
  if (!existsSync(path)) {
    throw new UsageError(`no gate signal at ${path}`, {
      hint: `Hermes writes this file when the gate is decided. Check the run id, or decide the gate first: hermes workflow gate ${runId} ${gateId} --decide approve`,
    });
  }
  let raw: unknown;
  try {
    raw = JSON.parse(readFileSync(path, 'utf8'));
  } catch (cause) {
    throw new UsageError(`gate signal at ${path} is not readable JSON`, {
      hint: 'This file is written by Hermes; a corrupt one means the run directory was edited by hand.',
      cause,
    });
  }
  const record = (typeof raw === 'object' && raw !== null ? raw : {}) as Record<string, unknown>;
  return {
    gate_id: String(record['gate_id'] ?? gateId),
    status: String(record['status'] ?? 'unknown'),
    ...(typeof record['decision'] === 'string'
      ? { decision: record['decision'] as GateDecision }
      : {}),
    ...(typeof record['note'] === 'string' ? { note: record['note'] } : {}),
  };
}

export interface StampOptions {
  plan: ExecutionPlan;
  signal: GateSignal;
  now?: string;
}

/**
 * Map a Hermes gate decision onto the plan's approval block.
 *
 * `modify` becomes `denied`, not `approved`: "come back with changes" is a
 * refusal of *this* plan, and the provisioner must not install it. The regenerated
 * plan gets its own approval.
 */
export function stampApproval(options: StampOptions): ExecutionPlan {
  const { signal } = options;
  if (signal.status !== 'decided' || signal.decision === undefined) {
    throw new UsageError(
      `gate ${signal.gate_id} is still ${signal.status} — no decision recorded`,
      {
        hint: `Decide it first: hermes workflow gate <run_id> ${signal.gate_id} --decide approve|shelve|modify`,
      },
    );
  }

  const decision =
    signal.decision === 'approve'
      ? 'approved'
      : signal.decision === 'shelve'
        ? 'shelved'
        : 'denied';

  return parseExecutionPlan({
    ...options.plan,
    approval: {
      ...options.plan.approval,
      decision,
      decided_at: options.now ?? nowIso(),
      ...(signal.note ? { note: signal.note } : {}),
    },
  });
}
