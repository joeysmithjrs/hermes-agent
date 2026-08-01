import { execFileSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { briefWarning, checkEdgeFirstBrief, EDGE_FIRST_BRIEF_SECTIONS } from '../src/plan/brief.js';
import { parseExecutionPlan, type ExecutionPlan } from '../src/schema/execution-plan.js';

/** A minimal valid plan; only telegram_brief matters for these tests. */
function planWithBrief(brief: string): ExecutionPlan {
  return parseExecutionPlan({
    version: 1,
    plan_id: 'plan_brief',
    paper_only: true,
    created_at: '2026-08-01T06:00:00.000Z',
    seed: { seed_id: 's', run_date: '2026-08-01', taxonomy_version: 1 },
    thesis: {
      title: 't',
      edge_mechanism: 'e',
      market_refs: [{ market_id: 'm' }],
      horizon: '1d',
      invalidations: ['x'],
      why_not_retail_hopium: 'y',
    },
    research_summary: {
      source_graph: [{ source: 's', authority: 'primary', claim: 'c' }],
      rules_resolution: 'r',
    },
    monitors: [],
    no_monitors_reason: 'nothing observable yet',
    hermes_setup: [],
    telegram_brief: brief,
    approval: { required: true, dual_control: true, channel: 'telegram', decision: 'pending' },
    paper_only_constraints: { live_execution_allowed: false, fill_type_if_ledger: 'SIMULATED_NO_FILL' },
  });
}

describe('edge-first brief contract', () => {
  const GOOD_BRIEF = `PAPER ONLY.
CLAIM: CPI 3.4 bucket is underpriced vs the nowcast residual distribution.
WHY GAP CAN EXIST: the market prices the point nowcast, not the residual spread.
MEASURED: p_bucket 0.62, rmse 0.18, N 154, mid 0.43, edge_vs_mid 0.19.
KILLS: BLS revises the methodology; nowcast vintage is look-ahead.
IF YOU APPROVE: installs one primary_source_change monitor. No order, no wallet.`;

  it('passes when all five sections are present (case-insensitive)', () => {
    const check = checkEdgeFirstBrief(GOOD_BRIEF);
    expect(check.ok).toBe(true);
    expect(check.missing).toEqual([]);
  });

  it('lists the missing sections when the brief is prose-only', () => {
    const check = checkEdgeFirstBrief('PAPER ONLY. Watch the CPI market, it looks interesting.');
    expect(check.ok).toBe(false);
    expect(check.missing).toEqual([...EDGE_FIRST_BRIEF_SECTIONS]);
  });

  it('reports only the sections actually missing', () => {
    const check = checkEdgeFirstBrief(
      'CLAIM: edge. MEASURED: p 0.6. IF YOU APPROVE: installs a monitor.',
    );
    expect(check.ok).toBe(false);
    expect(check.missing).toEqual(['WHY GAP CAN EXIST', 'KILLS']);
  });

  it('briefWarning returns null for a complete brief and a line otherwise', () => {
    expect(briefWarning(planWithBrief(GOOD_BRIEF))).toBeNull();
    const warning = briefWarning(planWithBrief('just prose'));
    expect(warning).toContain('missing');
    expect(warning).toContain('CLAIM');
  });
});

describe('plan validate --strict-brief (CLI)', () => {
  const cli = join(import.meta.dirname, '..', 'src', 'cli', 'pm-desk.ts');
  const env = Object.fromEntries(
    Object.entries(process.env).filter(([k]) => k !== 'PM_DESK_LIVE_CPI'),
  );

  function writePlan(brief: string): string {
    const dir = mkdtempSync(join(tmpdir(), 'pm-desk-brief-'));
    const file = join(dir, 'plan.json');
    writeFileSync(file, JSON.stringify(planWithBrief(brief)), 'utf8');
    return file;
  }

  function runCli(args: string[]): { status: number; stdout: string; stderr: string } {
    try {
      const stdout = execFileSync(process.execPath, ['--import', 'tsx', cli, ...args], {
        encoding: 'utf8',
        env,
        timeout: 30_000,
      });
      return { status: 0, stdout, stderr: '' };
    } catch (err) {
      const e = err as { status?: number; stdout?: string; stderr?: string; message?: string };
      return { status: e.status ?? 1, stdout: e.stdout ?? '', stderr: e.stderr ?? e.message ?? '' };
    }
  }

  const GOOD = `CLAIM: x. WHY GAP CAN EXIST: y. MEASURED: z. KILLS: a. IF YOU APPROVE: b.`;

  it('warns (but exits 0) when sections are missing and --strict-brief is off', () => {
    const file = writePlan('just prose, no sections');
    const out = runCli(['plan', 'validate', '--file', file, '--json']);
    expect(out.status, out.stderr).toBe(0);
    const payload = JSON.parse(out.stdout) as { brief_ok: boolean; brief_missing: string[] };
    expect(payload.brief_ok).toBe(false);
    expect(payload.brief_missing.length).toBe(5);
  });

  it('hard-fails under --strict-brief when a section is missing', () => {
    const file = writePlan('CLAIM: x. MEASURED: z.');
    const out = runCli(['plan', 'validate', '--file', file, '--strict-brief', '--json']);
    expect(out.status).not.toBe(0);
    expect(out.stderr).toContain('edge-first');
  });

  it('passes under --strict-brief when all sections are present', () => {
    const file = writePlan(GOOD);
    const out = runCli(['plan', 'validate', '--file', file, '--strict-brief', '--json']);
    expect(out.status, out.stderr).toBe(0);
    const payload = JSON.parse(out.stdout) as { brief_ok: boolean; strict_brief: boolean };
    expect(payload.brief_ok).toBe(true);
    expect(payload.strict_brief).toBe(true);
  });
});
