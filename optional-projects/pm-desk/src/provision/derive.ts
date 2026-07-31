import { join } from 'node:path';

import { sha256Hex } from '../core/hash.js';
import { packageWorkflowPath, ADJUDICATION_WORKFLOW_FILE } from '../hermes/package-paths.js';
import { parseMonitorSpec } from '../schema/monitor-spec.js';
import { parseSourceSpec } from '../schema/source-spec.js';
import type { ExecutionPlan, PlanMonitor } from '../schema/execution-plan.js';

/**
 * Turning an approved ExecutionPlan into an exact list of files to write and
 * argv arrays to spawn. Pure: nothing here touches the disk, the network or a
 * process, which is what lets `provision dry-run` and `provision apply` be the
 * same computation with one of them stopping short.
 *
 * The load-bearing rule is that NO string written by an agent is ever executed.
 * The plan's `hermes_setup[].apply_command` is display text — it is what Joe
 * read before approving. The argv the provisioner spawns is derived here from
 * the monitors, which are validated data. When the two disagree, that is
 * reported as drift; it never changes what runs.
 */

export type ActionKind =
  /** Copy this package's prompt libraries into $HERMES_HOME/workflows/prompts. */
  | 'install_prompts'
  /** `hermes cron create ...` for one schedule bucket of this plan's monitors. */
  | 'cron_create'
  /** `hermes workflow register ...` for the adjudication workflow. */
  | 'workflow_register'
  /** Printed for the operator to run (or not) themselves. Never executed. */
  | 'recipe';

export interface DerivedAction {
  kind: ActionKind;
  idempotency_key: string;
  /** The exact argv the provisioner would spawn, or [] for in-process/recipe work. */
  argv: string[];
  /** One-line render of `argv`, used for display and for the drift check. */
  display: string;
  /** For cron actions: the `--name` the job is created with, so revoke can find it. */
  cron_job_name?: string;
  notes: string;
}

export interface DerivedFile {
  path: string;
  contents: string;
  kind: 'plan' | 'monitor_spec' | 'source_spec' | 'sweep_script' | 'hermes_script';
  /** Sweep scripts must be executable; spec JSON must not be. */
  mode: number;
}

/** A setup item the plan declared that the provisioner will not execute. */
export interface DeclaredOnlyItem {
  action: string;
  idempotency_key: string;
  apply_command: string;
  reason: string;
}

/** A declared command that does not match what the provisioner would run. */
export interface CommandDrift {
  idempotency_key: string;
  declared: string;
  derived: string;
}

export interface ScheduleBucket {
  /** Filename-safe, stable, unique within the plan. */
  slug: string;
  schedule: string;
  monitors: PlanMonitor[];
  /** Where this bucket's monitor specs live under the desk home. */
  specs_dir: string;
  /** The script name inside $HERMES_HOME/scripts/. */
  script_name: string;
  notify: boolean;
}

export interface DerivedProvision {
  plan_id: string;
  plan_hash: string;
  plan_dir: string;
  buckets: ScheduleBucket[];
  files: DerivedFile[];
  actions: DerivedAction[];
  declared_only: DeclaredOnlyItem[];
  drift: CommandDrift[];
}

export interface DeriveOptions {
  plan: ExecutionPlan;
  /** $HERMES_HOME the operator passed. Never defaulted from the environment here. */
  hermesHome: string;
  /** $PM_DESK_HOME — where specs, the plan copy and the store live. */
  deskHome: string;
  /**
   * Absolute path to this package, baked into the generated sweep scripts. A
   * cron job runs from an arbitrary cwd, so the script cannot find the CLI by
   * relative path.
   */
  packageDir: string;
  /** Optional: node binary path, for a host where `node` is not on cron's PATH. */
  nodeBin?: string;
  hermesBin?: string;
}

export function deriveProvision(options: DeriveOptions): DerivedProvision {
  const { plan, hermesHome, deskHome, packageDir } = options;
  const hermesBin = options.hermesBin ?? 'hermes';

  const planDir = join(deskHome, 'plans', plan.plan_id);
  const files: DerivedFile[] = [];
  const actions: DerivedAction[] = [];

  files.push({
    path: join(planDir, 'plan.json'),
    contents: `${JSON.stringify(plan, null, 2)}\n`,
    kind: 'plan',
    mode: 0o644,
  });

  // Source specs are plan-wide (several monitors can share one source), so they
  // are written once outside the per-bucket directories.
  const sourcesDir = join(planDir, 'specs', 'sources');
  const seenSources = new Set<string>();
  for (const monitor of plan.monitors) {
    if (!monitor.source_spec_inline) continue;
    // Validated here rather than trusted: `source_spec_inline` is an opaque
    // object in the plan schema precisely so this is the place it gets checked.
    const spec = parseSourceSpec(monitor.source_spec_inline);
    if (seenSources.has(spec.id)) continue;
    seenSources.add(spec.id);
    files.push({
      path: join(sourcesDir, `${spec.id}.json`),
      contents: `${JSON.stringify(spec, null, 2)}\n`,
      kind: 'source_spec',
      mode: 0o644,
    });
  }

  const buckets = bucketize(plan, planDir);

  for (const bucket of buckets) {
    for (const monitor of bucket.monitors) {
      if (monitor.kind === 'custom_script') continue;
      const spec = parseMonitorSpec(monitor.spec_inline);
      files.push({
        path: join(bucket.specs_dir, `${spec.id}.json`),
        contents: `${JSON.stringify(spec, null, 2)}\n`,
        kind: 'monitor_spec',
        mode: 0o644,
      });
    }

    const script = sweepScript({
      plan,
      bucket,
      packageDir,
      deskHome,
      nodeBin: options.nodeBin ?? 'node',
    });
    // Two copies on purpose: the desk home keeps the auditable artifact, and
    // Hermes' cron scheduler only runs scripts that live under
    // $HERMES_HOME/scripts/ (verified against cron/scheduler.py, which rejects
    // anything resolving outside that directory).
    files.push({
      path: join(planDir, 'sweep', bucket.script_name),
      contents: script,
      kind: 'sweep_script',
      mode: 0o755,
    });
    files.push({
      path: join(hermesHome, 'scripts', bucket.script_name),
      contents: script,
      kind: 'hermes_script',
      mode: 0o755,
    });

    const cronName = `pm-desk-${plan.plan_id}-${bucket.slug}`;
    const argv = [
      'cron',
      'create',
      bucket.schedule,
      '--no-agent',
      '--script',
      bucket.script_name,
      '--name',
      cronName,
      // Without a delivery target a --no-agent job's stdout goes nowhere useful.
      // Only added when a monitor in this bucket actually asked to be notified.
      ...(bucket.notify ? ['--deliver', 'telegram'] : []),
    ];

    actions.push({
      kind: 'cron_create',
      idempotency_key: `${plan.plan_id}:cron:${bucket.slug}`,
      argv: [hermesBin, ...argv],
      display: [hermesBin, ...argv].join(' '),
      cron_job_name: cronName,
      notes: `sweeps ${bucket.monitors.length} monitor(s) every ${bucket.schedule}; silent stdout when nothing fires`,
    });
  }

  // Setup items that are not cron: matched by action, one derived action each.
  for (const item of plan.hermes_setup) {
    if (item.action === 'install_prompts') {
      actions.push({
        kind: 'install_prompts',
        idempotency_key: item.idempotency_key,
        argv: [],
        display: `pm-desk hermes install-prompts --hermes-home ${hermesHome} --apply`,
        notes: 'runs in-process; writes only this package’s prompt libraries',
      });
    } else if (item.action === 'workflow_register') {
      const catalogId = `pm-desk-adjudication`;
      const argv = [
        'workflow',
        'register',
        '--id',
        catalogId,
        '--from-file',
        packageWorkflowPath(ADJUDICATION_WORKFLOW_FILE),
        '--owner',
        'pm-desk',
        '--description',
        'PM Desk paper adjudication (one tools-empty agent node)',
      ];
      actions.push({
        kind: 'workflow_register',
        idempotency_key: item.idempotency_key,
        argv: [hermesBin, ...argv],
        display: [hermesBin, ...argv].join(' '),
        notes: 'registers the packaged adjudication workflow as a catalog recipe',
      });
    }
  }

  const { declared_only, drift } = reconcile(plan, actions, { hermesHome, deskHome });

  return {
    plan_id: plan.plan_id,
    plan_hash: sha256Hex(JSON.stringify(plan)),
    plan_dir: planDir,
    buckets,
    files,
    actions,
    declared_only,
    drift,
  };
}

/**
 * Group monitors by schedule. One cron job per distinct schedule, because
 * `hermes cron create --script` takes no script arguments — the bucket has to be
 * baked into the script, so each bucket needs its own file.
 */
function bucketize(plan: ExecutionPlan, planDir: string): ScheduleBucket[] {
  const bySchedule = new Map<string, PlanMonitor[]>();
  for (const monitor of plan.monitors) {
    const existing = bySchedule.get(monitor.schedule);
    if (existing) existing.push(monitor);
    else bySchedule.set(monitor.schedule, [monitor]);
  }

  const used = new Set<string>();
  const buckets: ScheduleBucket[] = [];
  for (const [schedule, monitors] of [...bySchedule.entries()].sort(([a], [b]) =>
    a.localeCompare(b),
  )) {
    let slug = schedule
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, '-')
      .replace(/^-+|-+$/g, '');
    if (slug === '') slug = 'sched';
    // Two different schedules can slug identically (`0 9 * * *` and `0-9-----`).
    // A hash suffix keeps the filenames distinct rather than silently merging
    // two cron jobs into one.
    if (used.has(slug)) slug = `${slug}-${sha256Hex(schedule).slice(0, 6)}`;
    used.add(slug);

    buckets.push({
      slug,
      schedule,
      monitors,
      specs_dir: join(planDir, 'buckets', slug, 'monitors'),
      script_name: `pm-desk-${plan.plan_id}-${slug}.sh`,
      notify: monitors.some((m) => m.on_fire.includes('notify_telegram')),
    });
  }
  return buckets;
}

/**
 * Compare what the plan told Joe against what the provisioner will do.
 *
 * Anything the plan declared that has no derived counterpart is `declared_only`
 * — reported and skipped, not run. Anything that matches but whose command text
 * differs is `drift` — also reported; the derived argv still wins.
 */
function reconcile(
  plan: ExecutionPlan,
  actions: DerivedAction[],
  homes: { hermesHome: string; deskHome: string },
): { declared_only: DeclaredOnlyItem[]; drift: CommandDrift[] } {
  const byKey = new Map(actions.map((a) => [a.idempotency_key, a]));
  const declared_only: DeclaredOnlyItem[] = [];
  const drift: CommandDrift[] = [];

  for (const item of plan.hermes_setup) {
    if (item.action === 'webhook_subscribe_recipe' || item.action === 'catalog_run') {
      declared_only.push({
        action: item.action,
        idempotency_key: item.idempotency_key,
        apply_command: item.apply_command,
        reason:
          item.action === 'webhook_subscribe_recipe'
            ? 'enabling a Hermes webhook edits your Hermes config, which pm-desk never does — run it yourself if you want it'
            : 'running a catalog workflow spends model budget; that is an operator decision, not a provisioning step',
      });
      continue;
    }
    const derived = byKey.get(item.idempotency_key);
    if (!derived) {
      declared_only.push({
        action: item.action,
        idempotency_key: item.idempotency_key,
        apply_command: item.apply_command,
        reason:
          'no derived action matches this idempotency_key — the plan asked for something the provisioner does not build from its monitors',
      });
      continue;
    }
    if (!sameCommand(expandPlaceholders(item.apply_command, homes), derived.display)) {
      drift.push({
        idempotency_key: item.idempotency_key,
        declared: item.apply_command,
        derived: derived.display,
      });
    }
  }

  return { declared_only, drift };
}

/**
 * Whitespace-insensitive, and the leading binary is compared by basename:
 * `--hermes-bin /opt/hermes-agent/venv/bin/hermes` is the same command as
 * `hermes`, and reporting that as drift would train an operator to ignore the
 * warning. Every other token must match exactly.
 */
function sameCommand(a: string, b: string): boolean {
  const norm = (value: string) => {
    const tokens = value.trim().split(/\s+/);
    if (tokens[0]) tokens[0] = tokens[0].slice(tokens[0].lastIndexOf('/') + 1);
    return tokens.join(' ');
  };
  return norm(a) === norm(b);
}

/**
 * The generator writes a plan hours before an operator picks the directories it
 * will be provisioned into, so it cannot know them. These placeholders are the
 * documented way for a plan to say "the home you pass on the command line", and
 * expanding them before the drift check keeps a correct plan from being
 * reported as drifted.
 */
export const PLACEHOLDERS = ['<HERMES_HOME>', '<DESK_HOME>'] as const;

function expandPlaceholders(
  command: string,
  homes: { hermesHome: string; deskHome: string },
): string {
  return command
    .replaceAll('<HERMES_HOME>', homes.hermesHome)
    .replaceAll('<DESK_HOME>', homes.deskHome);
}

interface SweepScriptOptions {
  plan: ExecutionPlan;
  bucket: ScheduleBucket;
  packageDir: string;
  deskHome: string;
  nodeBin: string;
}

/**
 * The per-bucket sweep. Adapted from `scripts/monitor-sweep.sh`, narrowed to one
 * plan's specs and with every path baked in — a cron job inherits neither this
 * package's cwd nor its environment.
 *
 * The contract that matters is the first one in the header: no output when
 * nothing fires. Paired with `hermes cron create --no-agent`, whose documented
 * behaviour is that empty stdout produces no message, a quiet desk costs nothing
 * and notifies nobody.
 */
function sweepScript(options: SweepScriptOptions): string {
  const { plan, bucket, packageDir, deskHome, nodeBin } = options;
  const monitorLines = bucket.monitors.map(
    (m) => `#   ${m.monitor_id}  (${m.kind}, ${m.priority}) -> ${m.on_fire.join(', ')}`,
  );
  const adjudicating = bucket.monitors.filter((m) => m.on_fire.includes('optional_adjudicate'));

  return `#!/usr/bin/env bash
#
# PM Desk — provisioned monitor sweep. GENERATED FILE, DO NOT EDIT.
#
#   plan      ${plan.plan_id}
#   thesis    ${plan.thesis.title.replace(/\n/g, ' ')}
#   schedule  ${bucket.schedule}
#   approved  ${plan.approval.decided_at ?? '(unrecorded)'}
#
# Monitors in this bucket:
${monitorLines.join('\n')}
#
# PAPER ONLY. This script reads public data and the local desk store. It cannot
# place an order, sign anything, or touch a wallet — there is no code path from
# here to a venue.
#
# It prints NOTHING when nothing fires. That is the whole design: paired with
# \`hermes cron create --no-agent\`, empty stdout means no notification, so a
# quiet desk costs zero tokens and sends zero messages.
#
# Regenerate with:  pm-desk provision apply --plan <plan.json> ...
# Remove with:      pm-desk provision revoke --plan-id ${plan.plan_id} ...
set -euo pipefail

PM_DESK_DIR=${shq(packageDir)}
PM_DESK_HOME=${shq(deskHome)}
MONITOR_DIR=${shq(bucket.specs_dir)}
NODE_BIN=${shq(nodeBin)}
CLI=("$NODE_BIN" --import tsx "$PM_DESK_DIR/src/cli/pm-desk.ts")

cd "$PM_DESK_DIR"

signals=$("\${CLI[@]}" monitor evaluate --dir "$MONITOR_DIR" --home "$PM_DESK_HOME" --json)

# --json prints [] when nothing fired, one object for a single signal, or an
# array for several. Count without needing jq on the host.
count=$(printf '%s' "$signals" | "$NODE_BIN" -e '
  let s = "";
  process.stdin.on("data", (d) => (s += d));
  process.stdin.on("end", () => {
    const v = JSON.parse(s || "[]");
    process.stdout.write(String(Array.isArray(v) ? v.length : 1));
  });
')

# The common case, and the point of the whole thing.
[ "$count" = "0" ] && exit 0

printf 'PM DESK — %s signal(s) fired (PAPER ONLY, no order can result)\\n' "$count"
printf 'plan   ${plan.plan_id}\\n'
printf 'thesis ${plan.thesis.title.replace(/'/g, "'\\\\''").replace(/\n/g, ' ')}\\n'
printf '\\n'

# Hand each signal to the desk's local loopback ingress if it is running: it
# validates, records BEFORE dispatching, and enforces idempotency. If it is not
# running, the evaluation above already recorded everything durably in the
# store, so say that rather than failing the cron job.
port="\${PM_DESK_INGRESS_PORT:-8787}"
if curl -sf "http://127.0.0.1:\${port}/health" >/dev/null 2>&1; then
  tmp="\${TMPDIR:-/tmp}/pm-desk-${plan.plan_id}-$$.json"
  printf '%s' "$signals" > "$tmp"
  "\${CLI[@]}" ingress submit --file "$tmp" --home "$PM_DESK_HOME" || true
  rm -f "$tmp"
else
  printf 'local ingress is not running on 127.0.0.1:%s — signals are recorded in the store; inspect with \`pm-desk ingress outbox\`\\n' "$port"
fi
${
  adjudicating.length > 0
    ? `
# ${adjudicating.map((m) => m.monitor_id).join(', ')} asked for adjudication on fire.
# This script does not start a workflow. Adjudication happens only if you run
# the ingress with the opt-in Hermes launcher:
#   pm-desk ingress serve --dispatch hermes --home "$PM_DESK_HOME"
# Otherwise adjudicate deliberately:
#   pm-desk workflow render --signal <signal_id> --home "$PM_DESK_HOME"
`
    : ''
}
printf '%s\\n' "$signals"
`;
}

/** Single-quote a value for bash. */
function shq(value: string): string {
  return `'${value.replace(/'/g, `'\\''`)}'`;
}
