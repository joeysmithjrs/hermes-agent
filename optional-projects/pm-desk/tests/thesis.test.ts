import { execFileSync } from 'node:child_process';
import { existsSync, mkdtempSync, readFileSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { REOPEN_WORKFLOW_FILE, packageWorkflowPath } from '../src/hermes/package-paths.js';

const cli = join(import.meta.dirname, '..', 'src', 'cli', 'pm-desk.ts');

describe('pm-desk thesis reopen', () => {
  let home: string;

  beforeEach(() => {
    home = mkdtempSync(join(tmpdir(), 'pm-desk-thesis-'));
  });

  afterEach(() => {
    rmSync(home, { recursive: true, force: true });
  });

  function runReopen(args: string[]): { status: number; stdout: string; stderr: string } {
    try {
      const stdout = execFileSync(process.execPath, ['--import', 'tsx', cli, ...args], {
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

  it('packs a prior-run pointer + focus and prints the run command', () => {
    const out = runReopen([
      'thesis',
      'reopen',
      '--prior-run',
      'wf_prior_123',
      '--focus-token',
      'tok_abc',
      '--focus-market',
      'mkt_1',
      '--question',
      'Will CPI YoY print in the 3.4 bucket?',
      '--bucket',
      '3.4',
      '--mid',
      '0.43',
      '--what-changed',
      'harness shipped: pm-desk research cpi-calibrate',
      '--json',
    ]);
    expect(out.status, out.stderr).toBe(0);

    const payload = JSON.parse(out.stdout) as {
      paper_only: boolean;
      prior_run_id: string;
      pack_path: string;
      workflow_file: string;
      run_command: string;
    };
    expect(payload.paper_only).toBe(true);
    expect(payload.prior_run_id).toBe('wf_prior_123');
    expect(payload.workflow_file).toBe(REOPEN_WORKFLOW_FILE);
    expect(payload.run_command).toContain(packageWorkflowPath(REOPEN_WORKFLOW_FILE));
    expect(payload.run_command).toContain('--input');

    // The pack file is the workflow input, written under the pm-desk workspace.
    expect(existsSync(payload.pack_path)).toBe(true);
    const pack = JSON.parse(readFileSync(payload.pack_path, 'utf8')) as {
      paper_only: boolean;
      prior_run_id: string;
      focus: { token_id: string; market_id?: string; question?: string; bucket?: number; mid?: number };
      banned_buildouts: string[];
      shipped_tools: string[];
    };
    expect(pack.paper_only).toBe(true);
    expect(pack.prior_run_id).toBe('wf_prior_123');
    expect(pack.focus.token_id).toBe('tok_abc');
    expect(pack.focus.market_id).toBe('mkt_1');
    expect(pack.focus.bucket).toBe(3.4);
    expect(pack.focus.mid).toBe(0.43);
    // The reopen must never re-propose the shipped harness.
    expect(pack.banned_buildouts).toContain('cpi_nowcast_bucket_harness');
    expect(pack.shipped_tools.some((s) => s.includes('cpi-calibrate'))).toBe(true);
  });

  it('requires --prior-run and --focus-token', () => {
    const out = runReopen(['thesis', 'reopen', '--json']);
    expect(out.status).not.toBe(0);
    expect(out.stderr).toMatch(/prior-run|focus-token/);
  });

  it('rejects an out-of-range --mid', () => {
    const out = runReopen([
      'thesis',
      'reopen',
      '--prior-run',
      'wf_1',
      '--focus-token',
      'tok_1',
      '--mid',
      '1.5',
      '--json',
    ]);
    expect(out.status).not.toBe(0);
    expect(out.stderr).toContain('--mid');
  });
});
