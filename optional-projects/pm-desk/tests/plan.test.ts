import { execFileSync } from 'node:child_process';
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { UsageError } from '../src/core/errors.js';
import { readGateSignal, stampApproval } from '../src/plan/approval.js';
import { planFromRun } from '../src/plan/from-run.js';
import { renderPlanSummary, renderPlanTelegram } from '../src/plan/render.js';
import { parseExecutionPlan, type ExecutionPlan } from '../src/schema/execution-plan.js';

const FIXTURE = join(import.meta.dirname, '..', 'fixtures', 'plans', 'example_execution_plan.json');
const CLI = join(import.meta.dirname, '..', 'src', 'cli', 'pm-desk.ts');

function fixturePlan(): ExecutionPlan {
  return parseExecutionPlan(JSON.parse(readFileSync(FIXTURE, 'utf8')));
}

describe('the shipped example plan', () => {
  it('validates, so the fixture cannot rot away from the schema', () => {
    const plan = fixturePlan();
    expect(plan.paper_only).toBe(true);
    expect(plan.monitors.length).toBeGreaterThan(0);
  });

  it('has an idempotency key per setup item and a spec per declarative monitor', () => {
    const plan = fixturePlan();
    for (const monitor of plan.monitors) {
      if (monitor.kind !== 'custom_script') expect(monitor.spec_inline).toBeDefined();
    }
    expect(new Set(plan.hermes_setup.map((s) => s.idempotency_key)).size).toBe(
      plan.hermes_setup.length,
    );
  });
});

describe('renderPlanTelegram', () => {
  it('fits one Telegram message', () => {
    expect(renderPlanTelegram(fixturePlan()).length).toBeLessThanOrEqual(4096);
  });

  it('leads with PAPER ONLY and says approval installs nothing tradeable', () => {
    const text = renderPlanTelegram(fixturePlan());
    expect(text.split('\n')[0]).toContain('PAPER ONLY');
    expect(text).toContain('places no order');
  });

  it('prints every hermes_setup command verbatim — the approval covers the argv', () => {
    const plan = fixturePlan();
    const text = renderPlanTelegram(plan);
    for (const item of plan.hermes_setup) {
      expect(text).toContain(item.apply_command);
    }
  });

  it('keeps the commands even when the brief has to be truncated', () => {
    const plan = fixturePlan();
    const bloated = parseExecutionPlan({
      ...plan,
      telegram_brief: `${plan.telegram_brief}\n${'padding padding padding. '.repeat(150)}`.slice(
        0,
        4096,
      ),
    });
    const text = renderPlanTelegram(bloated);
    expect(text.length).toBeLessThanOrEqual(4096);
    for (const item of bloated.hermes_setup) {
      expect(text).toContain(item.apply_command);
    }
    expect(text).toContain('APPROVAL');
  });

  it('names every monitor that will be installed', () => {
    const plan = fixturePlan();
    const text = renderPlanTelegram(plan);
    for (const monitor of plan.monitors) expect(text).toContain(monitor.monitor_id);
  });

  it('says so plainly when a plan installs nothing', () => {
    const plan = fixturePlan();
    const empty = parseExecutionPlan({
      ...plan,
      monitors: [],
      hermes_setup: [],
      no_monitors_reason: 'Nothing observable changes before this market resolves.',
    });
    const text = renderPlanTelegram(empty);
    expect(text).toContain('MONITORS TO INSTALL (0)');
    expect(text).toContain('Nothing observable changes');
  });

  it('splits buildouts: joe_* under NEEDS YOUR OK, auto_agent under AGENT SHOULD EXECUTE', () => {
    const plan = parseExecutionPlan({
      ...fixturePlan(),
      proposed_buildouts: [
        {
          id: 'paid_datafeed',
          kind: 'datafeed_or_api',
          title: 'Paid CPI feed',
          problem: 'need it',
          proposed_interface: 'api',
          validation_plan: 'dry-run',
          cost_risk_notes: 'costs money',
          spawn_recommendation: 'none',
          approval_class: 'joe_infra',
          approval_required: true,
          decision: 'pending',
        },
        {
          id: 'loader_fix',
          kind: 'collector_script',
          title: 'Fix the Cleveland loader',
          problem: 'broken',
          proposed_interface: 'patch',
          validation_plan: 'tests',
          cost_risk_notes: 'in-repo code',
          spawn_recommendation: 'none',
          approval_class: 'auto_agent',
          approval_required: false,
          decision: 'pending',
        },
      ],
    });
    const text = renderPlanTelegram(plan);
    // Joe-gated buildout surfaces under the needs-OK header.
    expect(text).toContain('NEEDS YOUR OK');
    expect(text).toContain('paid_datafeed');
    expect(text).toContain('joe_infra');
    // auto_agent buildout is NOT framed as needing Joe.
    expect(text).toContain('AGENT SHOULD EXECUTE');
    expect(text).toContain('loader_fix');
    // The needs-OK block (up to the AGENT header) carries the joe buildout only.
    const needsOkBlock = text.slice(
      text.indexOf('NEEDS YOUR OK'),
      text.indexOf('AGENT SHOULD EXECUTE'),
    );
    expect(needsOkBlock).toContain('paid_datafeed');
    expect(needsOkBlock).not.toContain('loader_fix');
  });
});

describe('renderPlanSummary', () => {
  it('shows both the dry-run and the apply command for every setup item', () => {
    const plan = fixturePlan();
    const text = renderPlanSummary(plan);
    for (const item of plan.hermes_setup) {
      expect(text).toContain(item.dry_run_command);
      expect(text).toContain(item.apply_command);
    }
    expect(text).toContain('live_execution_allowed: false');
  });
});

describe('planFromRun', () => {
  let home: string;
  const runId = 'wfr_test_run';

  const writeNodeOutput = (nodeRunId: string, output: unknown) => {
    const dir = join(home, 'workflows', 'runs', runId, 'nodes', nodeRunId);
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, 'output.json'), JSON.stringify(output), 'utf8');
  };

  const writeWorkspaceFile = (name: string, body: string, workspace = 'pm-desk') => {
    const dir = join(home, 'workflows', 'workspaces', workspace, 'runs', runId);
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, name), body, 'utf8');
  };

  const writeWorkspacePlan = (plan: unknown) =>
    writeWorkspaceFile('execution_plan.json', JSON.stringify(plan, null, 2));

  const writeCheckpoint = (byNode: Record<string, string[]>) => {
    const dir = join(home, 'workflows', 'runs', runId);
    mkdirSync(dir, { recursive: true });
    writeFileSync(
      join(dir, 'checkpoint.json'),
      JSON.stringify({ node_runs_by_node: byNode }),
      'utf8',
    );
  };

  beforeEach(() => {
    home = mkdtempSync(join(tmpdir(), 'pm-desk-run-'));
  });

  afterEach(() => {
    rmSync(home, { recursive: true, force: true });
  });

  it('reads the plan out of an agent node output whose text is a JSON fence', () => {
    const plan = fixturePlan();
    writeCheckpoint({ plan: ['nr_plan_1'] });
    writeNodeOutput('nr_plan_1', {
      node: 'plan',
      text: `Here is the plan.\n\`\`\`json\n${JSON.stringify(plan)}\n\`\`\`\n`,
    });

    const found = planFromRun({ hermesHome: home, runId, nodeId: 'plan' });
    expect(found.plan.plan_id).toBe(plan.plan_id);
    expect(found.node_id).toBe('plan');
    expect(found.node_run_id).toBe('nr_plan_1');
  });

  it('reads a bare JSON object emitted with no fence', () => {
    const plan = fixturePlan();
    writeCheckpoint({ plan: ['nr_plan_1'] });
    writeNodeOutput('nr_plan_1', { node: 'plan', text: JSON.stringify(plan, null, 2) });
    expect(planFromRun({ hermesHome: home, runId }).plan.plan_id).toBe(plan.plan_id);
  });

  it('finds the plan without a checkpoint, by validating rather than guessing a path', () => {
    const plan = fixturePlan();
    writeNodeOutput('nr_dq', { node: 'dq', text: '{"decision":"advance"}' });
    writeNodeOutput('nr_plan', { node: 'plan', text: JSON.stringify(plan) });
    const found = planFromRun({ hermesHome: home, runId });
    expect(found.plan.plan_id).toBe(plan.plan_id);
    expect(found.node_id).toBeUndefined();
  });

  it('ignores non-plan node outputs instead of failing on them', () => {
    const plan = fixturePlan();
    writeNodeOutput('nr_prepare', { node: 'prepare', text: 'not json at all' });
    writeNodeOutput('nr_eval', { node: 'eval', text: '{"decision":"advance_one","rank":[1]}' });
    writeNodeOutput('nr_plan', { node: 'plan', text: JSON.stringify(plan) });
    expect(planFromRun({ hermesHome: home, runId }).plan.plan_id).toBe(plan.plan_id);
  });

  it('surfaces the validation error when the only candidate is a malformed plan', () => {
    const plan = fixturePlan();
    writeNodeOutput('nr_plan', {
      node: 'plan',
      text: JSON.stringify({ ...plan, paper_only: false }),
    });
    expect(() => planFromRun({ hermesHome: home, runId })).toThrow(/no valid ExecutionPlan/);
    try {
      planFromRun({ hermesHome: home, runId });
    } catch (err) {
      expect((err as UsageError).hint).toContain('paper_only');
    }
  });

  it('recovers the plan the agent wrote to the workspace when its text was markdown', () => {
    // The wf_9e6e868d97f1 shape: a valid plan on disk, prose in the envelope.
    const plan = fixturePlan();
    writeCheckpoint({ plan: ['nr_plan_1'] });
    writeNodeOutput('nr_plan_1', {
      node: 'plan',
      text: '## Execution plan\n\n- Wrote the plan to the workspace.\n- Monitors: 2\n',
    });
    writeWorkspacePlan(plan);

    const found = planFromRun({ hermesHome: home, runId });
    expect(found.plan.plan_id).toBe(plan.plan_id);
    expect(found.source_kind).toBe('workspace_artifact');
    expect(found.source_path).toContain(join('workspaces', 'pm-desk', 'runs', runId));
    expect(found.node_run_id).toBeUndefined();
  });

  it('recovers from the workspace when the run produced no node outputs at all', () => {
    const plan = fixturePlan();
    writeWorkspacePlan(plan);
    expect(planFromRun({ hermesHome: home, runId }).plan.plan_id).toBe(plan.plan_id);
  });

  it('prefers the node envelope over the workspace artifact when both parse', () => {
    const plan = fixturePlan();
    writeNodeOutput('nr_plan', { node: 'plan', text: JSON.stringify(plan) });
    writeWorkspacePlan({ ...plan, plan_id: 'plan_stale_from_disk' });

    const found = planFromRun({ hermesHome: home, runId });
    expect(found.plan.plan_id).toBe(plan.plan_id);
    expect(found.source_kind).toBe('node_output');
  });

  it('reads a plan an agent wrapped in a fence inside the workspace file', () => {
    const plan = fixturePlan();
    writeWorkspaceFile(
      'execution_plan.json',
      `\`\`\`json\n${JSON.stringify(plan, null, 2)}\n\`\`\`\n`,
    );
    expect(planFromRun({ hermesHome: home, runId }).plan.plan_id).toBe(plan.plan_id);
  });

  it('reports the workspace it searched when neither layer holds a plan', () => {
    writeNodeOutput('nr_plan', { node: 'plan', text: 'no json here' });
    try {
      planFromRun({ hermesHome: home, runId });
      throw new Error('expected planFromRun to throw');
    } catch (err) {
      expect((err as UsageError).hint).toContain(join('workspaces', 'pm-desk', 'runs', runId));
    }
  });

  it('honours a non-default workspace name', () => {
    const plan = fixturePlan();
    writeNodeOutput('nr_plan', { node: 'plan', text: 'no json here' });
    writeWorkspaceFile('execution_plan.json', JSON.stringify(plan), 'other-desk');

    // The default workspace is a different directory and must not be searched.
    expect(() => planFromRun({ hermesHome: home, runId })).toThrow(/no valid ExecutionPlan/);
    expect(planFromRun({ hermesHome: home, runId, workspace: 'other-desk' }).plan.plan_id).toBe(
      plan.plan_id,
    );
  });

  it('does not promote an invalid workspace artifact', () => {
    const plan = fixturePlan();
    writeWorkspacePlan({ ...plan, paper_only: false });
    expect(() => planFromRun({ hermesHome: home, runId })).toThrow(/no valid ExecutionPlan/);
  });

  it('refuses to choose when a run holds two different plans', () => {
    const plan = fixturePlan();
    writeNodeOutput('nr_a', { node: 'plan', text: JSON.stringify(plan) });
    writeNodeOutput('nr_b', {
      node: 'plan_alt',
      text: JSON.stringify({ ...plan, plan_id: 'plan_other' }),
    });
    expect(() => planFromRun({ hermesHome: home, runId })).toThrow(/2 different ExecutionPlans/);
  });

  it('accepts a retried node that produced the same plan twice', () => {
    const plan = fixturePlan();
    writeNodeOutput('nr_a', { node: 'plan', text: JSON.stringify(plan) });
    writeNodeOutput('nr_b', { node: 'plan', text: JSON.stringify(plan) });
    expect(planFromRun({ hermesHome: home, runId }).plan.plan_id).toBe(plan.plan_id);
  });

  it('explains a --dry-run run, which stores no node outputs', () => {
    mkdirSync(join(home, 'workflows', 'runs', runId), { recursive: true });
    expect(() => planFromRun({ hermesHome: home, runId })).toThrow(/no node outputs/);
  });

  it('names the directory it looked in when the run does not exist', () => {
    expect(() => planFromRun({ hermesHome: home, runId: 'nope' })).toThrow(
      /no Hermes run directory/,
    );
  });
});

describe('gate-derived approval', () => {
  let home: string;
  const runId = 'wfr_gate_run';

  const writeGate = (body: unknown) => {
    const dir = join(home, 'workflows', 'runs', runId, 'gate_signals');
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, 'paper_gate.json'), JSON.stringify(body), 'utf8');
  };

  beforeEach(() => {
    home = mkdtempSync(join(tmpdir(), 'pm-desk-gate-'));
  });

  afterEach(() => {
    rmSync(home, { recursive: true, force: true });
  });

  it('approves only from a decision Hermes itself recorded', () => {
    writeGate({ gate_id: 'paper_gate', decision: 'approve', note: 'ok', status: 'decided' });
    const stamped = stampApproval({
      plan: fixturePlan(),
      signal: readGateSignal(home, runId, 'paper_gate'),
      now: '2026-07-31T07:00:00.000Z',
    });
    expect(stamped.approval.decision).toBe('approved');
    expect(stamped.approval.decided_at).toBe('2026-07-31T07:00:00.000Z');
    expect(stamped.approval.note).toBe('ok');
  });

  it('treats a modify as a refusal of this plan, not an approval', () => {
    writeGate({ gate_id: 'paper_gate', decision: 'modify', status: 'decided' });
    const stamped = stampApproval({
      plan: fixturePlan(),
      signal: readGateSignal(home, runId, 'paper_gate'),
    });
    expect(stamped.approval.decision).toBe('denied');
  });

  it('maps a shelve to shelved', () => {
    writeGate({ gate_id: 'paper_gate', decision: 'shelve', status: 'decided' });
    expect(
      stampApproval({ plan: fixturePlan(), signal: readGateSignal(home, runId, 'paper_gate') })
        .approval.decision,
    ).toBe('shelved');
  });

  it('refuses to stamp a gate that is still pending', () => {
    writeGate({ gate_id: 'paper_gate', status: 'pending' });
    expect(() =>
      stampApproval({ plan: fixturePlan(), signal: readGateSignal(home, runId, 'paper_gate') }),
    ).toThrow(/still pending/);
  });

  it('points at the exact file when the gate signal is missing', () => {
    expect(() => readGateSignal(home, runId, 'paper_gate')).toThrow(/no gate signal at/);
  });
});

describe('plan after-gate (CLI)', () => {
  let home: string;
  const runId = 'wfr_after_gate';

  beforeEach(() => {
    home = mkdtempSync(join(tmpdir(), 'pm-desk-aftergate-'));
  });
  afterEach(() => {
    rmSync(home, { recursive: true, force: true });
  });

  function runCli(args: string[]): { status: number; stdout: string; stderr: string } {
    try {
      const stdout = execFileSync(process.execPath, ['--import', 'tsx', CLI, ...args], {
        encoding: 'utf8',
        env: { ...process.env, HERMES_HOME: home },
        timeout: 30_000,
      });
      return { status: 0, stdout, stderr: '' };
    } catch (err) {
      const e = err as { status?: number; stdout?: string; stderr?: string; message?: string };
      return { status: e.status ?? 1, stdout: e.stdout ?? '', stderr: e.stderr ?? e.message ?? '' };
    }
  }

  it('reports the monitors count and the install command for an approved plan', () => {
    const plan = fixturePlan();
    // Lay out a run dir with a plan node output the recoverer can read.
    const nodeDir = join(home, 'workflows', 'runs', runId, 'nodes', 'nr_plan_1');
    mkdirSync(nodeDir, { recursive: true });
    writeFileSync(
      join(nodeDir, 'output.json'),
      JSON.stringify({ node: 'plan', text: JSON.stringify({ ...plan, approval: { ...plan.approval, decision: 'approved', decided_at: '2026-08-01T07:00:00.000Z' } }) }),
      'utf8',
    );

    const out = runCli(['plan', 'after-gate', '--run-id', runId, '--json']);
    expect(out.status, out.stderr).toBe(0);
    const payload = JSON.parse(out.stdout) as {
      plan_id: string;
      monitors: number;
      approved: boolean;
      next_command: string;
    };
    expect(payload.plan_id).toBe(plan.plan_id);
    expect(payload.monitors).toBe(plan.monitors.length);
    expect(payload.approved).toBe(true);
    expect(payload.next_command).toContain('provision apply');
  });

  it('says nothing installs for a plan with no monitors', () => {
    const plan = fixturePlan();
    const empty = { ...plan, monitors: [], hermes_setup: [], no_monitors_reason: 'nothing observable yet' };
    const nodeDir = join(home, 'workflows', 'runs', runId, 'nodes', 'nr_plan_1');
    mkdirSync(nodeDir, { recursive: true });
    writeFileSync(join(nodeDir, 'output.json'), JSON.stringify({ node: 'plan', text: JSON.stringify(empty) }), 'utf8');

    const out = runCli(['plan', 'after-gate', '--run-id', runId, '--json']);
    expect(out.status, out.stderr).toBe(0);
    const payload = JSON.parse(out.stdout) as { monitors: number; next_command: string };
    expect(payload.monitors).toBe(0);
    expect(payload.next_command).toContain('nothing installs');
  });
});
