import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';

import { UsageError } from '../core/errors.js';
import { parseExecutionPlan, type ExecutionPlan } from '../schema/execution-plan.js';

/**
 * Pull the ExecutionPlan back out of a finished Hermes run.
 *
 * The generator workflow ends at a gate, so the plan exists only as a node
 * output inside `$HERMES_HOME/workflows/runs/<run_id>/`. Hermes has no
 * "give me node X's output" CLI, so this reads the run store's on-disk layout
 * directly. That layout was checked on this host against `workflow/store/fs.py`:
 *
 *   runs/<run_id>/checkpoint.json          node_runs_by_node: {node_id: [node_run_id]}
 *   runs/<run_id>/nodes/<node_run_id>/output.json
 *
 * Node outputs are keyed by `node_run_id`, never by `node_id` — a retried or
 * fanned-out node has several — which is why the checkpoint is consulted first
 * rather than guessing a directory name. When the checkpoint is missing or does
 * not name the node, every node output is scanned instead and the plan is
 * identified by the only thing that cannot be faked: it validates.
 *
 * SECOND PLACE TO LOOK: the workspace. The plan prompt tells the model to
 * write_file the plan to `{{ workspace.run_dir }}/execution_plan.json` before
 * answering. On run wf_9e6e868d97f1 it did exactly that — and then wrote
 * markdown in its chat text, so the node envelope held no JSON at all, the
 * output schema failed, and a perfectly good plan sat on disk unreachable.
 * The node envelope still wins when it parses; the workspace is what turns
 * that morning from a total loss into a plan an operator can still gate.
 *
 * This is read-only. Nothing here writes into a Hermes home.
 */

export interface PlanFromRunOptions {
  hermesHome: string;
  runId: string;
  /** The node that emits the plan. Defaults to the generator's `plan` node. */
  nodeId?: string;
  /** The named workspace the generator pins. Defaults to `pm-desk`. */
  workspace?: string;
}

export interface PlanFromRunResult {
  plan: ExecutionPlan;
  /** Where it was found, so an operator can go look at the raw output. */
  source_path: string;
  /** Which layer it came from — the envelope, or an artifact the agent wrote. */
  source_kind: 'node_output' | 'workspace_artifact';
  node_run_id?: string;
  node_id?: string;
}

export const DEFAULT_PLAN_NODE_ID = 'plan';
export const DEFAULT_WORKSPACE = 'pm-desk';

/** The artifact names the plan prompt is told to write, most specific first. */
const WORKSPACE_PLAN_FILES = ['execution_plan.json', 'plan.json'] as const;

export function runDir(hermesHome: string, runId: string): string {
  return join(hermesHome, 'workflows', 'runs', runId);
}

export function workspaceRunDir(hermesHome: string, workspace: string, runId: string): string {
  return join(hermesHome, 'workflows', 'workspaces', workspace, 'runs', runId);
}

export function planFromRun(options: PlanFromRunOptions): PlanFromRunResult {
  const dir = runDir(options.hermesHome, options.runId);
  const workspace = options.workspace ?? DEFAULT_WORKSPACE;
  const workspaceDir = workspaceRunDir(options.hermesHome, workspace, options.runId);
  if (!existsSync(dir) && !existsSync(workspaceDir)) {
    throw new UsageError(`no Hermes run directory at ${dir}`, {
      hint: 'Check --run-id and --hermes-home. `hermes workflow list` shows run ids.',
    });
  }

  const nodeId = options.nodeId ?? DEFAULT_PLAN_NODE_ID;
  const candidates = candidateOutputs(dir, nodeId);
  const failures: string[] = [];
  const found: PlanFromRunResult[] = [];
  for (const candidate of candidates) {
    const raw = readJson(candidate.path);
    if (raw === undefined) continue;
    for (const body of extractJsonBodies(raw)) {
      const result = tryParse(body);
      if (result.ok) {
        found.push({
          plan: result.plan,
          source_path: candidate.path,
          source_kind: 'node_output',
          node_run_id: candidate.nodeRunId,
          ...(candidate.nodeId ? { node_id: candidate.nodeId } : {}),
        });
      } else {
        failures.push(`${candidate.nodeId ?? candidate.nodeRunId}: ${result.message}`);
      }
    }
  }

  // The envelope is preferred when it parses: it is what the gate saw. Only
  // when it holds no plan at all does the artifact on disk get its turn, so a
  // stale file can never quietly outrank what the node actually emitted.
  if (found.length === 0) {
    const artifact = planFromWorkspace(workspaceDir, failures);
    if (artifact) return artifact;
  }

  // Only when nothing anywhere was even examined — a --dry-run leaves the run
  // directory empty. A rejected workspace artifact is a validation story, not
  // an empty-run one, so it falls through to the message below.
  if (candidates.length === 0 && failures.length === 0) {
    throw new UsageError(`run ${options.runId} has no node outputs to read`, {
      hint: `Looked under ${join(dir, 'nodes')} and ${workspaceDir}. A --dry-run run stores neither; the plan only exists after the plan node actually ran.`,
    });
  }

  if (found.length === 0) {
    const searched = `No plan artifact under ${workspaceDir} either.`;
    throw new UsageError(`no valid ExecutionPlan found in run ${options.runId}`, {
      hint:
        failures.length > 0
          ? `Closest candidates failed validation — ${failures.slice(0, 3).join(' | ')}. ${searched} Fix the plan node's prompt or hand-correct the JSON and use \`pm-desk plan validate --file\`.`
          : `Node outputs exist but none contained JSON. ${searched} Inspect ${join(dir, 'nodes')} directly.`,
    });
  }

  // Several identical plans (a retried node) are fine; genuinely different ones
  // mean the caller must say which node they meant rather than us picking.
  const distinct = new Set(found.map((f) => f.plan.plan_id));
  if (distinct.size > 1) {
    throw new UsageError(
      `run ${options.runId} contains ${distinct.size} different ExecutionPlans: ${[...distinct].join(', ')}`,
      { hint: 'Pass --node <node_id> to name the one you want.' },
    );
  }
  return found[0]!;
}

/**
 * The plan the agent wrote into this run's workspace directory. Scoped to the
 * run's own directory on purpose: the cross-run `last_execution_plan.json` at
 * the workspace top level belongs to whichever morning finished most recently,
 * and handing an operator yesterday's plan under today's run id would be worse
 * than telling them nothing was found.
 */
function planFromWorkspace(
  workspaceDir: string,
  failures: string[],
): PlanFromRunResult | undefined {
  if (!existsSync(workspaceDir)) return undefined;
  for (const name of WORKSPACE_PLAN_FILES) {
    const path = join(workspaceDir, name);
    if (!existsSync(path)) continue;
    // Loose, not strict: the file is whatever the agent's write_file produced,
    // which is sometimes a fenced block rather than bare JSON.
    const raw = readLooseJson(path);
    if (raw === undefined) continue;
    for (const body of extractJsonBodies(raw)) {
      const result = tryParse(body);
      if (result.ok) {
        return { plan: result.plan, source_path: path, source_kind: 'workspace_artifact' };
      }
      failures.push(`${name}: ${result.message}`);
    }
  }
  return undefined;
}

interface Candidate {
  path: string;
  nodeRunId: string;
  nodeId?: string;
}

/** Node outputs to inspect: the named node's runs first, then everything else. */
function candidateOutputs(dir: string, nodeId: string): Candidate[] {
  const nodesDir = join(dir, 'nodes');
  if (!existsSync(nodesDir)) return [];

  const byNode = checkpointNodeRuns(dir);
  const preferred = new Set(byNode[nodeId] ?? []);
  const reverse = new Map<string, string>();
  for (const [id, runs] of Object.entries(byNode)) {
    for (const nodeRunId of runs) reverse.set(nodeRunId, id);
  }

  const all: Candidate[] = [];
  for (const entry of readdirSync(nodesDir).sort()) {
    const path = join(nodesDir, entry, 'output.json');
    if (!existsSync(path)) continue;
    const nodeIdForRun = reverse.get(entry);
    all.push({ path, nodeRunId: entry, ...(nodeIdForRun ? { nodeId: nodeIdForRun } : {}) });
  }

  return [
    ...all.filter((c) => preferred.has(c.nodeRunId)),
    ...all.filter((c) => !preferred.has(c.nodeRunId)),
  ];
}

function checkpointNodeRuns(dir: string): Record<string, string[]> {
  const raw = readJson(join(dir, 'checkpoint.json'));
  if (raw === undefined || typeof raw !== 'object' || raw === null) return {};
  const byNode = (raw as Record<string, unknown>)['node_runs_by_node'];
  if (typeof byNode !== 'object' || byNode === null) return {};
  const out: Record<string, string[]> = {};
  for (const [key, value] of Object.entries(byNode as Record<string, unknown>)) {
    if (Array.isArray(value)) out[key] = value.filter((v): v is string => typeof v === 'string');
  }
  return out;
}

function readLooseJson(path: string): unknown {
  try {
    return parseLooseJson(readFileSync(path, 'utf8'));
  } catch {
    return undefined;
  }
}

function readJson(path: string): unknown {
  try {
    return JSON.parse(readFileSync(path, 'utf8'));
  } catch {
    return undefined;
  }
}

/**
 * An agent node's stored output is
 * `{text, node, result, effective_model, effective_provider}` — the model's
 * answer lives in `text` as a string. So the plan can arrive as: the output
 * object itself, the `result` value, or JSON embedded in `text` (very often
 * inside a ```json fence, whatever the prompt asked for).
 */
function extractJsonBodies(raw: unknown): unknown[] {
  const bodies: unknown[] = [];
  if (typeof raw === 'object' && raw !== null) {
    const record = raw as Record<string, unknown>;
    bodies.push(record);
    if (record['result'] !== undefined) bodies.push(record['result']);
    for (const key of ['text', 'output'] as const) {
      const value = record[key];
      if (typeof value === 'string') {
        const parsed = parseLooseJson(value);
        if (parsed !== undefined) bodies.push(parsed);
      } else if (typeof value === 'object' && value !== null) {
        bodies.push(value);
      }
    }
  } else if (typeof raw === 'string') {
    const parsed = parseLooseJson(raw);
    if (parsed !== undefined) bodies.push(parsed);
  }
  return bodies;
}

/** Parse JSON that a model may have wrapped in a fence or surrounded with prose. */
function parseLooseJson(text: string): unknown {
  const trimmed = text.trim();
  const fenced = /```(?:json)?\s*([\s\S]*?)```/.exec(trimmed);
  const body = fenced?.[1]?.trim() ?? trimmed;
  const direct = attempt(body);
  if (direct !== undefined) return direct;

  const start = body.indexOf('{');
  const end = body.lastIndexOf('}');
  if (start >= 0 && end > start) return attempt(body.slice(start, end + 1));
  return undefined;
}

function attempt(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return undefined;
  }
}

type ParseOutcome = { ok: true; plan: ExecutionPlan } | { ok: false; message: string };

function tryParse(body: unknown): ParseOutcome {
  if (typeof body !== 'object' || body === null) return { ok: false, message: 'not an object' };
  // Only report a validation failure for things that were clearly meant to be a
  // plan; every agent output object would otherwise produce noise.
  const record = body as Record<string, unknown>;
  const looksLikeAPlan =
    'thesis' in record || 'plan_id' in record || 'paper_only_constraints' in record;
  try {
    return { ok: true, plan: parseExecutionPlan(body) };
  } catch (err) {
    if (!looksLikeAPlan) return { ok: false, message: 'not a plan' };
    return { ok: false, message: err instanceof Error ? err.message : String(err) };
  }
}
