import { existsSync, mkdtempSync, readFileSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { packageRoot } from '../src/hermes/package-paths.js';
import type { CommandResult } from '../src/ingress/dispatcher.js';
import { deriveProvision } from '../src/provision/derive.js';
import { parseCronJobId, provision } from '../src/provision/provisioner.js';
import { provisionStatus, revokePlan } from '../src/provision/revoke.js';
import { parseExecutionPlan, type ExecutionPlan } from '../src/schema/execution-plan.js';
import { openStore, type DeskStore } from '../src/store/index.js';

/**
 * The provisioner is the only part of this package that creates cron jobs and
 * writes into a Hermes home, so every test here uses temp directories and a
 * fake command runner. Nothing in this file can reach a real $HERMES_HOME or
 * spawn a real `hermes`.
 */

const FIXTURE = join(import.meta.dirname, '..', 'fixtures', 'plans', 'example_execution_plan.json');

function fixturePlan(): ExecutionPlan {
  return parseExecutionPlan(JSON.parse(readFileSync(FIXTURE, 'utf8')));
}

function approvedPlan(): ExecutionPlan {
  const plan = fixturePlan();
  return parseExecutionPlan({
    ...plan,
    approval: { ...plan.approval, decision: 'approved', decided_at: '2026-07-31T07:00:00.000Z' },
  });
}

/** Records every argv it is asked to run and never spawns anything. */
function fakeRunner(responses: Record<string, CommandResult> = {}) {
  const calls: string[][] = [];
  const runner = async (command: string, args: string[]): Promise<CommandResult> => {
    calls.push([command, ...args]);
    const key = args.slice(0, 2).join(' ');
    return responses[key] ?? { code: 0, stdout: 'Created job: job_abc123\n', stderr: '' };
  };
  return { calls, runner };
}

describe('deriveProvision', () => {
  const base = () => ({
    plan: approvedPlan(),
    hermesHome: '/tmp/fake-hermes',
    deskHome: '/tmp/fake-desk',
    packageDir: '/opt/pm-desk',
  });

  it('is pure — deriving twice gives identical files and argv', () => {
    const a = deriveProvision(base());
    const b = deriveProvision(base());
    expect(a.files).toEqual(b.files);
    expect(a.actions).toEqual(b.actions);
    expect(a.plan_hash).toBe(b.plan_hash);
  });

  it('creates one cron job per distinct schedule, not one per monitor', () => {
    const derived = deriveProvision(base());
    const crons = derived.actions.filter((a) => a.kind === 'cron_create');
    // The fixture has three monitors across two schedules (30m, 1h).
    expect(derived.plan_id).toBeTruthy();
    expect(crons).toHaveLength(2);
    expect(derived.buckets.map((b) => b.schedule).sort()).toEqual(['1h', '30m']);
  });

  it('writes each bucket its own spec directory so a sweep sees only its monitors', () => {
    const derived = deriveProvision(base());
    const specs = derived.files.filter((f) => f.kind === 'monitor_spec');
    const dirs = new Set(specs.map((f) => f.path.replace(/\/[^/]+$/, '')));
    expect(dirs.size).toBe(2);
    for (const bucket of derived.buckets) {
      const inBucket = specs.filter((f) => f.path.startsWith(bucket.specs_dir));
      expect(inBucket).toHaveLength(bucket.monitors.length);
    }
  });

  it('puts the sweep script under $HERMES_HOME/scripts, where the cron scheduler looks', () => {
    const derived = deriveProvision(base());
    const hermesScripts = derived.files.filter((f) => f.kind === 'hermes_script');
    expect(hermesScripts.length).toBeGreaterThan(0);
    for (const file of hermesScripts) {
      expect(file.path.startsWith('/tmp/fake-hermes/scripts/')).toBe(true);
      expect(file.mode).toBe(0o755);
    }
  });

  it('passes only the bare script name to `hermes cron create`', () => {
    // The scheduler resolves a relative --script under $HERMES_HOME/scripts and
    // rejects anything resolving outside it, so an absolute path here would be
    // both redundant and fragile across homes.
    for (const action of deriveProvision(base()).actions) {
      if (action.kind !== 'cron_create') continue;
      const script = action.argv[action.argv.indexOf('--script') + 1]!;
      expect(script).not.toContain('/');
      expect(script.endsWith('.sh')).toBe(true);
    }
  });

  it('asks for a delivery target only when a monitor wanted to be notified', () => {
    const plan = approvedPlan();
    const quiet = parseExecutionPlan({
      ...plan,
      monitors: plan.monitors.map((m) => ({ ...m, on_fire: ['queue_ingress'] })),
    });
    const derived = deriveProvision({ ...base(), plan: quiet });
    for (const action of derived.actions.filter((a) => a.kind === 'cron_create')) {
      expect(action.argv).not.toContain('--deliver');
    }
    expect(
      deriveProvision(base())
        .actions.filter((a) => a.kind === 'cron_create')
        .every((a) => a.argv.includes('--deliver')),
    ).toBe(true);
  });

  it('validates spec_inline and source_spec_inline rather than trusting them', () => {
    const plan = approvedPlan();
    const broken = parseExecutionPlan({
      ...plan,
      monitors: [{ ...plan.monitors[0]!, spec_inline: { id: 'x', kind: 'not_a_kind' } }],
    });
    expect(() => deriveProvision({ ...base(), plan: broken })).toThrow(/MonitorSpec/);

    const badSource = parseExecutionPlan({
      ...plan,
      monitors: [{ ...plan.monitors[0]!, source_spec_inline: { id: 'x', url: 'http://insecure' } }],
    });
    expect(() => deriveProvision({ ...base(), plan: badSource })).toThrow(/SourceSpec/);
  });

  it('never executes a command string the plan wrote — webhooks stay recipes', () => {
    const derived = deriveProvision(base());
    expect(derived.actions.some((a) => a.kind === 'recipe')).toBe(false);
    const webhook = derived.declared_only.find((d) => d.action === 'webhook_subscribe_recipe');
    expect(webhook).toBeDefined();
    expect(webhook?.reason).toContain('never does');
  });

  it('reports drift when the plan showed a command different from the derived one', () => {
    const plan = approvedPlan();
    const lying = parseExecutionPlan({
      ...plan,
      hermes_setup: plan.hermes_setup.map((s) =>
        s.action === 'cron_create'
          ? { ...s, apply_command: 'hermes cron create 5m --no-agent --script somethingelse.sh' }
          : s,
      ),
    });
    const derived = deriveProvision({ ...base(), plan: lying });
    expect(derived.drift.length).toBeGreaterThan(0);
    // Drift is reported, but the derived argv is unchanged by it.
    for (const action of derived.actions.filter((a) => a.kind === 'cron_create')) {
      expect(action.display).not.toContain('somethingelse.sh');
    }
  });

  it('expands <HERMES_HOME> in a declared command before calling it drift', () => {
    // A plan is written before the operator picks a home, so the placeholder is
    // the correct thing for it to say.
    expect(deriveProvision(base()).drift).toEqual([]);
  });

  it('does not call an absolute --hermes-bin drift from the plan’s bare `hermes`', () => {
    // Reporting that would train an operator to ignore the warning.
    expect(
      deriveProvision({ ...base(), hermesBin: '/opt/hermes-agent/venv/bin/hermes' }).drift,
    ).toEqual([]);
  });

  it('flags a declared setup item with no derived counterpart instead of running it', () => {
    const plan = approvedPlan();
    const extra = parseExecutionPlan({
      ...plan,
      hermes_setup: [
        ...plan.hermes_setup,
        {
          action: 'cron_create',
          dry_run_command: 'hermes cron create 5m --no-agent --script ghost.sh',
          apply_command: 'hermes cron create 5m --no-agent --script ghost.sh',
          idempotency_key: 'ghost',
        },
      ],
    });
    const derived = deriveProvision({ ...base(), plan: extra });
    expect(derived.declared_only.some((d) => d.idempotency_key === 'ghost')).toBe(true);
    expect(derived.actions.some((a) => a.display.includes('ghost.sh'))).toBe(false);
  });

  it('keeps two schedules that slug identically as two distinct jobs', () => {
    const plan = approvedPlan();
    const collide = parseExecutionPlan({
      ...plan,
      monitors: [
        { ...plan.monitors[0]!, monitor_id: 'a', schedule: '0 9 * * *' },
        {
          ...plan.monitors[1]!,
          monitor_id: 'b',
          schedule: '0-9-*-*-*',
          spec_inline: { ...plan.monitors[1]!.spec_inline, id: 'b' },
        },
      ],
      hermes_setup: [],
    });
    const derived = deriveProvision({ ...base(), plan: collide });
    const slugs = derived.buckets.map((b) => b.slug);
    expect(new Set(slugs).size).toBe(2);
    expect(
      new Set(derived.files.filter((f) => f.kind === 'hermes_script').map((f) => f.path)).size,
    ).toBe(2);
  });

  it('bakes absolute paths into the sweep script, because cron inherits no cwd', () => {
    const derived = deriveProvision(base());
    const script = derived.files.find((f) => f.kind === 'sweep_script')!.contents;
    expect(script).toContain("PM_DESK_DIR='/opt/pm-desk'");
    expect(script).toContain("PM_DESK_HOME='/tmp/fake-desk'");
    expect(script).toContain('set -euo pipefail');
    // The silent-when-quiet contract.
    expect(script).toContain('[ "$count" = "0" ] && exit 0');
    expect(script).toContain('PAPER ONLY');
    // It must not start a workflow on its own.
    expect(script).not.toContain('workflow run');
  });
});

describe('provision dry-run', () => {
  let deskHome: string;
  let hermesHome: string;
  let store: DeskStore;

  beforeEach(() => {
    deskHome = mkdtempSync(join(tmpdir(), 'pm-desk-prov-'));
    hermesHome = mkdtempSync(join(tmpdir(), 'pm-desk-hermes-'));
    store = openStore({ home: deskHome });
  });

  afterEach(() => {
    store.close();
    rmSync(deskHome, { recursive: true, force: true });
    rmSync(hermesHome, { recursive: true, force: true });
  });

  it('writes no files and spawns no commands', async () => {
    const { calls, runner } = fakeRunner();
    const result = await provision({
      plan: fixturePlan(),
      store,
      hermesHome,
      deskHome,
      packageDir: packageRoot(),
      mode: 'dry-run',
      runner,
    });

    expect(calls).toEqual([]);
    expect(result.files.every((f) => f.status === 'planned')).toBe(true);
    for (const file of result.files) expect(existsSync(file.path)).toBe(false);
    expect(existsSync(join(hermesHome, 'scripts'))).toBe(false);
  });

  it('previews a plan that is still pending approval', async () => {
    const plan = fixturePlan();
    expect(plan.approval.decision).toBe('pending');
    const result = await provision({
      plan,
      store,
      hermesHome,
      deskHome,
      packageDir: packageRoot(),
      mode: 'dry-run',
      runner: fakeRunner().runner,
    });
    expect(result.ok).toBe(true);
    expect(result.actions.length).toBeGreaterThan(0);
  });

  it('records the plan so status can report on it before anything is applied', async () => {
    await provision({
      plan: fixturePlan(),
      store,
      hermesHome,
      deskHome,
      packageDir: packageRoot(),
      mode: 'dry-run',
      runner: fakeRunner().runner,
    });
    const status = provisionStatus(store, fixturePlan().plan_id);
    expect(status.status).toBe('dry_run');
    expect(status.approval_decision).toBe('pending');
    expect(status.artifacts).toEqual([]);
  });
});

describe('provision apply', () => {
  let deskHome: string;
  let hermesHome: string;
  let store: DeskStore;

  const applyOnce = async (overrides: Record<string, unknown> = {}) => {
    const { calls, runner } = fakeRunner();
    const result = await provision({
      plan: approvedPlan(),
      store,
      hermesHome,
      deskHome,
      packageDir: packageRoot(),
      mode: 'apply',
      acknowledged: true,
      runner,
      ...overrides,
    });
    return { calls, result };
  };

  beforeEach(() => {
    deskHome = mkdtempSync(join(tmpdir(), 'pm-desk-prov-'));
    hermesHome = mkdtempSync(join(tmpdir(), 'pm-desk-hermes-'));
    store = openStore({ home: deskHome });
  });

  afterEach(() => {
    store.close();
    rmSync(deskHome, { recursive: true, force: true });
    rmSync(hermesHome, { recursive: true, force: true });
  });

  it('refuses a plan that is not approved', async () => {
    await expect(
      provision({
        plan: fixturePlan(),
        store,
        hermesHome,
        deskHome,
        packageDir: packageRoot(),
        mode: 'apply',
        acknowledged: true,
        runner: fakeRunner().runner,
      }),
    ).rejects.toThrow(/approval is 'pending'/);
  });

  it('refuses without the explicit acknowledgement, even for an approved plan', async () => {
    await expect(
      provision({
        plan: approvedPlan(),
        store,
        hermesHome,
        deskHome,
        packageDir: packageRoot(),
        mode: 'apply',
        acknowledged: false,
        runner: fakeRunner().runner,
      }),
    ).rejects.toThrow(/i-approved-this-plan/);
  });

  it('refuses a plan that is not paper-only, whatever the approval says', async () => {
    // parseExecutionPlan cannot build this, so it is forged past the schema —
    // which is exactly the case the second check exists for.
    const forged = { ...approvedPlan(), paper_only: false } as unknown as ExecutionPlan;
    await expect(
      provision({
        plan: forged,
        store,
        hermesHome,
        deskHome,
        packageDir: packageRoot(),
        mode: 'apply',
        acknowledged: true,
        runner: fakeRunner().runner,
      }),
    ).rejects.toThrow(/not paper-only/);
  });

  it('writes the specs, the sweep scripts and the plan copy', async () => {
    const { result } = await applyOnce();
    expect(result.ok).toBe(true);
    for (const file of result.files) {
      expect(existsSync(file.path), file.path).toBe(true);
    }
    const script = result.files.find((f) => f.kind === 'hermes_script')!;
    expect(statSync(script.path).mode & 0o111).toBeGreaterThan(0);
    expect(readFileSync(script.path, 'utf8')).toContain('PAPER ONLY');
  });

  it('spawns exactly the derived argv, as an array, one call per cron job', async () => {
    const { calls } = await applyOnce();
    const cronCalls = calls.filter((c) => c[1] === 'cron');
    expect(cronCalls).toHaveLength(2);
    for (const call of cronCalls) {
      expect(call[0]).toBe('hermes');
      expect(call.slice(1, 3)).toEqual(['cron', 'create']);
      expect(call).toContain('--no-agent');
      // No shell string anywhere: every element is a separate argv entry.
      expect(call.some((part) => part.includes('&&') || part.includes(';'))).toBe(false);
    }
  });

  it('installs the prompt libraries in-process, into the home it was given', async () => {
    await applyOnce();
    expect(existsSync(join(hermesHome, 'workflows', 'prompts'))).toBe(true);
  });

  it('is idempotent — a second apply spawns nothing and skips every action', async () => {
    await applyOnce();
    const { calls, result } = await applyOnce();
    expect(calls).toEqual([]);
    expect(result.actions.every((a) => a.status === 'skipped')).toBe(true);
    expect(result.files.every((f) => f.status === 'unchanged')).toBe(true);
  });

  it('records the cron job id it parsed, so revoke has something precise to remove', async () => {
    await applyOnce();
    const status = provisionStatus(store, approvedPlan().plan_id);
    const crons = status.actions.filter((a) => a.action === 'cron_create');
    expect(crons).toHaveLength(2);
    for (const cron of crons) {
      expect(cron.status).toBe('applied');
      expect(cron.job).toBe('job_abc123');
    }
  });

  it('marks the plan failed and reports the stderr when a command exits non-zero', async () => {
    const { runner } = fakeRunner({
      'cron create': { code: 2, stdout: '', stderr: 'cron: scheduler unavailable' },
    });
    const result = await provision({
      plan: approvedPlan(),
      store,
      hermesHome,
      deskHome,
      packageDir: packageRoot(),
      mode: 'apply',
      acknowledged: true,
      runner,
    });
    expect(result.ok).toBe(false);
    expect(result.actions.some((a) => a.reason?.includes('scheduler unavailable'))).toBe(true);
    expect(provisionStatus(store, approvedPlan().plan_id).status).toBe('failed');
  });

  it('retries a failed action on the next apply rather than treating it as done', async () => {
    const failing = fakeRunner({
      'cron create': { code: 1, stdout: '', stderr: 'transient' },
    });
    await provision({
      plan: approvedPlan(),
      store,
      hermesHome,
      deskHome,
      packageDir: packageRoot(),
      mode: 'apply',
      acknowledged: true,
      runner: failing.runner,
    });

    const retry = fakeRunner();
    const result = await provision({
      plan: approvedPlan(),
      store,
      hermesHome,
      deskHome,
      packageDir: packageRoot(),
      mode: 'apply',
      acknowledged: true,
      runner: retry.runner,
    });
    expect(retry.calls.filter((c) => c[1] === 'cron')).toHaveLength(2);
    expect(result.ok).toBe(true);
  });
});

describe('provision revoke', () => {
  let deskHome: string;
  let hermesHome: string;
  let store: DeskStore;

  beforeEach(async () => {
    deskHome = mkdtempSync(join(tmpdir(), 'pm-desk-prov-'));
    hermesHome = mkdtempSync(join(tmpdir(), 'pm-desk-hermes-'));
    store = openStore({ home: deskHome });
    await provision({
      plan: approvedPlan(),
      store,
      hermesHome,
      deskHome,
      packageDir: packageRoot(),
      mode: 'apply',
      acknowledged: true,
      runner: fakeRunner().runner,
    });
  });

  afterEach(() => {
    store.close();
    rmSync(deskHome, { recursive: true, force: true });
    rmSync(hermesHome, { recursive: true, force: true });
  });

  it('removes only the jobs recorded for this plan, by their recorded id', async () => {
    const { calls, runner } = fakeRunner();
    const result = await revokePlan({ store, planId: approvedPlan().plan_id, runner });

    expect(result.ok).toBe(true);
    expect(calls).toHaveLength(2);
    for (const call of calls) {
      expect(call.slice(0, 3)).toEqual(['hermes', 'cron', 'remove']);
      expect(call[3]).toBe('job_abc123');
    }
    expect(provisionStatus(store, approvedPlan().plan_id).status).toBe('revoked');
  });

  it('never lists or pattern-matches the operator’s other cron jobs', async () => {
    const { calls, runner } = fakeRunner();
    await revokePlan({ store, planId: approvedPlan().plan_id, runner });
    expect(calls.some((c) => c.includes('list'))).toBe(false);
  });

  it('leaves the artifacts in place and says so', async () => {
    const { runner } = fakeRunner();
    const result = await revokePlan({ store, planId: approvedPlan().plan_id, runner });
    expect(result.retained_artifacts.length).toBeGreaterThan(0);
    for (const path of result.retained_artifacts) expect(existsSync(path)).toBe(true);
  });

  it('previews with --dry-run without removing anything', async () => {
    const { calls, runner } = fakeRunner();
    const result = await revokePlan({
      store,
      planId: approvedPlan().plan_id,
      runner,
      dryRun: true,
    });
    expect(calls).toEqual([]);
    expect(result.jobs.every((j) => j.status === 'planned')).toBe(true);
    expect(provisionStatus(store, approvedPlan().plan_id).status).toBe('applied');
  });

  it('reports a failed removal instead of claiming the plan was revoked', async () => {
    const { runner } = fakeRunner({
      'cron remove': { code: 1, stdout: '', stderr: 'Job not found: job_abc123' },
    });
    const result = await revokePlan({ store, planId: approvedPlan().plan_id, runner });
    expect(result.ok).toBe(false);
    expect(result.jobs.some((j) => j.reason?.includes('Job not found'))).toBe(true);
    expect(provisionStatus(store, approvedPlan().plan_id).status).toBe('applied');
  });

  it('refuses to revoke a plan this desk never provisioned', async () => {
    await expect(revokePlan({ store, planId: 'plan_unknown' })).rejects.toThrow(
      /no provisioned plan/,
    );
  });
});

describe('parseCronJobId', () => {
  it('reads the id out of the colourised line hermes prints', () => {
    expect(parseCronJobId('[32mCreated job: job_7f2a[0m\n')).toBe('job_7f2a');
    expect(parseCronJobId('Created job: job_plain')).toBe('job_plain');
  });

  it('returns undefined rather than guessing when the line is absent', () => {
    expect(parseCronJobId('something else entirely')).toBeUndefined();
  });
});
