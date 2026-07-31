import { z } from 'zod';

import { objectHash } from '../core/hash.js';
import { deterministicId } from '../core/ids.js';
import { IsoTimestampSchema, Sha256HexSchema, SlugSchema, validate } from './common.js';

/**
 * The ExecutionPlan: the one artifact that carries a morning generator run from
 * "an agent had an idea" to "the desk is watching something".
 *
 * It is deliberately the *whole* contract between the two halves of the loop:
 *
 *   generator workflow  --emits-->  ExecutionPlan  --approved by Joe-->  provisioner
 *
 * Everything downstream of the gate is deterministic TypeScript reading this
 * object. No agent runs after the approval.
 *
 * Note what `hermes_setup[].apply_command` is and is not. It is the string Joe
 * is shown before he decides. It is NOT executed: the provisioner derives its
 * own argv from the monitors and spawns that via execFile, and reports any
 * entry whose declared string disagrees with the derived one as drift. A
 * command an agent wrote is a thing to display and check, never a thing to run.
 *
 * Two invariants are literals rather than booleans-with-a-default, so a plan
 * that permits live execution cannot be represented in this type at all:
 * `paper_only: true` and `paper_only_constraints.live_execution_allowed: false`.
 * A generator that emits `false` fails validation at the boundary and the
 * provisioner never sees it.
 */

/** A market this plan is about. All fields optional but at least one required. */
const MarketRefSchema = z
  .object({
    market_id: z.string().min(1).optional(),
    condition_id: z.string().min(1).optional(),
    token_id: z.string().min(1).optional(),
    question: z.string().min(1).max(500).optional(),
    outcome: z.enum(['YES', 'NO']).optional(),
  })
  .strict()
  .refine(
    (value) =>
      value.market_id !== undefined ||
      value.condition_id !== undefined ||
      value.token_id !== undefined ||
      value.question !== undefined,
    { message: 'a market_ref needs at least one of market_id, condition_id, token_id or question' },
  );

const SeedSchema = z
  .object({
    seed_id: z.string().min(1).max(200),
    run_date: z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'must be a YYYY-MM-DD run date'),
    taxonomy_version: z.number().int().positive(),
    desk_state_hash: Sha256HexSchema.optional(),
  })
  .strict();

const ThesisSchema = z
  .object({
    title: z.string().min(1).max(200),
    /**
     * Not "why is this mispriced" but "why is the mispricing available to THIS
     * desk" — a paper desk with read-only public data and no wallet. A thesis
     * that needs a market maker's inventory is not an edge this desk can hold.
     */
    edge_mechanism: z.string().min(1).max(2000),
    market_refs: z.array(MarketRefSchema).min(1).max(20),
    horizon: z.string().min(1).max(200),
    invalidations: z.array(z.string().min(1).max(500)).min(1).max(20),
    why_not_retail_hopium: z.string().min(1).max(2000),
  })
  .strict();

const SourceGraphEntrySchema = z
  .object({
    source: z.string().min(1).max(300),
    authority: z.enum(['primary', 'secondary', 'unknown']),
    claim: z.string().min(1).max(1000),
    url: z.string().url().optional(),
    observed_at: z.string().min(1).max(100).optional(),
  })
  .strict();

const ResearchSummarySchema = z
  .object({
    source_graph: z.array(SourceGraphEntrySchema).min(1).max(50),
    rules_resolution: z.string().min(1).max(4000),
    unknowns: z.array(z.string().min(1).max(500)).max(30).default([]),
    dq_notes: z.array(z.string().min(1).max(500)).max(30).default([]),
    eval_scorecard: z.record(z.string(), z.number()).optional(),
  })
  .strict();

/**
 * What a fired monitor is allowed to do. Everything here is notification or
 * bookkeeping; `paper_ledger_candidate` queues a candidate for the operator,
 * it does not write a fill (the ledger's own schema makes fills impossible).
 */
export const ON_FIRE_ACTIONS = [
  'notify_telegram',
  'queue_ingress',
  'paper_ledger_candidate',
  'optional_adjudicate',
] as const;

const MonitorScriptSchema = z
  .object({
    /**
     * Where the provisioner should place the per-plan sweep script, relative to
     * the desk home. A hint rather than an absolute path: the plan is written by
     * an agent that does not know the operator's directory layout, and the
     * provisioner is the only thing allowed to decide where files land.
     */
    path_hint: z.string().min(1).max(200),
    args: z.array(z.string().max(500)).max(40).default([]),
    /** Cron watchdog contract: no output means nothing fired means no message. */
    silent_when_empty: z.literal(true),
  })
  .strict();

const PlanMonitorSchema = z
  .object({
    monitor_id: SlugSchema,
    kind: z.enum([
      'primary_source_change',
      'market_move',
      'source_market_divergence',
      'custom_script',
    ]),
    /**
     * `script_first` is the default posture and the cheap one: a cron job runs
     * `pm-desk monitor evaluate` and stays silent unless a declarative rule
     * fires. `agent_reasoning` is only justified when the decision genuinely
     * needs a model, and it costs a workflow run every time it fires.
     */
    priority: z.enum(['script_first', 'agent_reasoning']),
    /** A MonitorSpec body, validated against MonitorSpecSchema by the provisioner. */
    spec_inline: z.record(z.string(), z.unknown()).optional(),
    /** A SourceSpec body, validated against SourceSpecSchema by the provisioner. */
    source_spec_inline: z.record(z.string(), z.unknown()).optional(),
    script: MonitorScriptSchema.optional(),
    /** A cron expression (`0 9 * * *`) or a Hermes interval (`30m`). */
    schedule: z.string().min(1).max(100),
    on_fire: z.array(z.enum(ON_FIRE_ACTIONS)).min(1).max(4),
    notes: z.string().max(1000).optional(),
  })
  .strict()
  .refine((value) => value.kind === 'custom_script' || value.spec_inline !== undefined, {
    message: 'a declarative monitor kind requires spec_inline (a MonitorSpec body)',
    path: ['spec_inline'],
  })
  .refine((value) => value.kind !== 'custom_script' || value.script !== undefined, {
    message: 'kind: custom_script requires a script block',
    path: ['script'],
  });

/**
 * The setup actions a plan can ask for. Each carries both commands as literal
 * strings so the Telegram brief can show Joe the exact thing that will run — an
 * approval on a summary nobody could check is not dual control, it is a rubber
 * stamp. The provisioner reads the action and derives the argv itself.
 */
export const HERMES_SETUP_ACTIONS = [
  'workflow_register',
  'cron_create',
  'webhook_subscribe_recipe',
  'install_prompts',
  'catalog_run',
] as const;

const HermesSetupSchema = z
  .object({
    action: z.enum(HERMES_SETUP_ACTIONS),
    dry_run_command: z.string().min(1).max(2000),
    apply_command: z.string().min(1).max(2000),
    /** Stable across regenerations of the same plan; the provisioner dedupes on it. */
    idempotency_key: z.string().min(1).max(200),
    notes: z.string().max(1000).optional(),
  })
  .strict();

const ApprovalSchema = z
  .object({
    required: z.literal(true),
    dual_control: z.literal(true),
    channel: z.literal('telegram'),
    decision: z.enum(['pending', 'approved', 'denied', 'shelved']),
    decided_at: IsoTimestampSchema.optional(),
    note: z.string().max(2000).optional(),
  })
  .strict()
  .refine((value) => value.decision === 'pending' || value.decided_at !== undefined, {
    message: 'a decided approval must carry decided_at',
    path: ['decided_at'],
  });

const PaperOnlyConstraintsSchema = z
  .object({
    live_execution_allowed: z.literal(false),
    fill_type_if_ledger: z.literal('SIMULATED_NO_FILL'),
  })
  .strict();

export const ExecutionPlanSchema = z
  .object({
    version: z.literal(1),
    plan_id: z.string().min(1).max(200),
    paper_only: z.literal(true),
    created_at: IsoTimestampSchema,
    /** The Hermes run that produced this plan, when it is known. */
    generator_run_id: z.string().min(1).max(200).optional(),
    seed: SeedSchema,
    thesis: ThesisSchema,
    research_summary: ResearchSummarySchema,
    monitors: z.array(PlanMonitorSchema).max(20).default([]),
    /**
     * Why a plan with nothing to watch is still worth recording. A generator
     * that finds a real story but no observable trigger should say so rather
     * than invent a monitor to satisfy a schema.
     */
    no_monitors_reason: z.string().min(1).max(1000).optional(),
    hermes_setup: z.array(HermesSetupSchema).max(30).default([]),
    telegram_brief: z.string().min(1).max(4096),
    approval: ApprovalSchema,
    paper_only_constraints: PaperOnlyConstraintsSchema,
  })
  .strict()
  .refine((value) => value.monitors.length > 0 || value.no_monitors_reason !== undefined, {
    message: 'a plan with no monitors must state no_monitors_reason',
    path: ['monitors'],
  })
  .refine(
    (value) => {
      const ids = value.monitors.map((m) => m.monitor_id);
      return new Set(ids).size === ids.length;
    },
    { message: 'monitor_id must be unique within a plan', path: ['monitors'] },
  )
  .refine(
    (value) => {
      const keys = value.hermes_setup.map((s) => s.idempotency_key);
      return new Set(keys).size === keys.length;
    },
    {
      message: 'hermes_setup idempotency_key must be unique within a plan',
      path: ['hermes_setup'],
    },
  );

export type ExecutionPlan = z.infer<typeof ExecutionPlanSchema>;
export type PlanMonitor = ExecutionPlan['monitors'][number];
export type HermesSetupItem = ExecutionPlan['hermes_setup'][number];
export type OnFireAction = (typeof ON_FIRE_ACTIONS)[number];

export function parseExecutionPlan(value: unknown): ExecutionPlan {
  return validate(ExecutionPlanSchema, value, 'ExecutionPlan');
}

/**
 * The same shape as a JSON Schema, for documentation and for a node's
 * `spec.output` block.
 *
 * `io: 'input'` because a generator writes the pre-parse form: defaults such as
 * `unknowns: []` are optional on the wire and only materialize after zod parses
 * them. Emitting the output form would tell an agent that fields it does not
 * have to send are required.
 *
 * Note the honest limit: cross-field `.refine()` rules (the uniqueness checks,
 * "no monitors needs a reason") have no JSON Schema representation and are
 * silently absent here. This export is a description; `parseExecutionPlan` is
 * the enforcement.
 */
export function executionPlanJsonSchema(): Record<string, unknown> {
  return z.toJSONSchema(ExecutionPlanSchema, { io: 'input' }) as Record<string, unknown>;
}

/**
 * A plan_id an agent cannot forge into a collision and a rerun reproduces.
 *
 * Derived from the seed and the thesis rather than from a timestamp, so
 * regenerating the same plan from the same seed lands on the same id — which is
 * what makes `provision apply` idempotent across a retried morning run.
 */
export function computePlanId(input: {
  seed_id: string;
  run_date: string;
  thesis_title: string;
  market_refs: readonly unknown[];
}): string {
  return deterministicId('plan', [
    input.run_date,
    input.seed_id,
    input.thesis_title,
    objectHash(input.market_refs),
  ]);
}
