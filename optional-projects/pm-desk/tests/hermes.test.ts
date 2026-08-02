import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { isAbsolute, join } from 'node:path';

import { parse as parseYaml } from 'yaml';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { ConfigError } from '../src/core/errors.js';
import {
  ADJUDICATION_WORKFLOW_FILE,
  GENERATOR_WORKFLOW_FILE,
  packagePromptDir,
  packageRoot,
  packageWorkflowPath,
  REOPEN_WORKFLOW_FILE,
  RESEARCH_WORKFLOW_FILE,
} from '../src/hermes/package-paths.js';
import {
  applyPromptInstall,
  planPromptInstall,
  resolveHermesHome,
  shippedPromptNames,
} from '../src/hermes/prompts.js';
import { HermesLauncherDispatcher } from '../src/ingress/dispatcher.js';
import { openStore } from '../src/store/index.js';

/**
 * The binding between this optional package and a Hermes install.
 *
 * Everything here is offline. The one test that runs the real `hermes` binary
 * is opt-in (PM_DESK_HERMES_INTEGRATION=1) because Hermes is a Python install
 * that this package's own CI does not provision — see the CI workflow.
 *
 * No test in this file may write to a real $HERMES_HOME. Every install test
 * targets an explicit temp directory.
 */

interface WorkflowDoc {
  workflow: string;
  workspace?: string;
  nodes: {
    id: string;
    kind: string;
    run?: string;
    over?: string;
    branch?: { kind: string; spec?: NodeSpec };
    spec?: NodeSpec;
  }[];
}

interface NodeSpec {
  tools?: string[];
  prompt?: string | { library?: string; params?: Record<string, unknown> };
  model?: string;
}

function loadWorkflow(file: string): WorkflowDoc {
  return parseYaml(readFileSync(packageWorkflowPath(file), 'utf8')) as WorkflowDoc;
}

interface DebateishNode {
  id: string;
  kind: string;
  participants?: { id?: string; kind?: string; spec?: NodeSpec }[];
  directive?: { judge?: { id?: string; kind?: string; spec?: NodeSpec } };
  branch?: { kind: string; spec?: NodeSpec };
  spec?: NodeSpec;
}

/** Every agent-bearing spec, including fanout branches and debate children. */
function agentSpecs(doc: WorkflowDoc): { id: string; spec: NodeSpec }[] {
  const out: { id: string; spec: NodeSpec }[] = [];
  for (const node of doc.nodes as DebateishNode[]) {
    if ((node.kind === 'agent' || node.kind === 'supervisor') && node.spec) {
      out.push({ id: node.id, spec: node.spec });
    }
    if (node.kind === 'fanout' && node.branch?.kind === 'agent' && node.branch.spec) {
      out.push({ id: `${node.id}.branch`, spec: node.branch.spec });
    }
    if (node.kind === 'debate') {
      for (const p of node.participants ?? []) {
        if (p.kind === 'agent' && p.spec) {
          out.push({ id: `${node.id}.participant.${p.id ?? 'anon'}`, spec: p.spec });
        }
      }
      const judge = node.directive?.judge;
      if (judge?.kind === 'agent' && judge.spec) {
        out.push({ id: `${node.id}.judge`, spec: judge.spec });
      }
    }
  }
  return out;
}

describe('Node engine preflight', () => {
  it('reads the required major out of a range that does not start with a digit', async () => {
    const { parseRequiredMajor, evaluate } = await import('../scripts/preflight.mjs');

    // The bug this guards: /[^\d].*$/ against ">=24.0.0" strips everything,
    // yielding 0, which every Node version trivially satisfies.
    expect(parseRequiredMajor('>=24.0.0')).toBe(24);
    expect(parseRequiredMajor('>=24')).toBe(24);
    expect(parseRequiredMajor('^24.1.0 || >=25')).toBe(24);
    expect(parseRequiredMajor('latest')).toBeNull();

    expect(evaluate('v22.23.1', '>=24.0.0').ok).toBe(false);
    expect(evaluate('v24.18.1', '>=24.0.0').ok).toBe(true);
    expect(evaluate('v25.0.0', '>=24.0.0').ok).toBe(true);
  });

  it('is the same floor the package declares, and the SDK requires', () => {
    const pkg = JSON.parse(readFileSync(join(packageRoot(), 'package.json'), 'utf8')) as {
      engines: { node: string };
      dependencies: Record<string, string>;
    };
    // Not a snapshot of a number: the assertion is that the package's declared
    // floor is at least the floor @polymarket/client declares for itself.
    const sdk = JSON.parse(
      readFileSync(
        join(packageRoot(), 'node_modules', '@polymarket', 'client', 'package.json'),
        'utf8',
      ),
    ) as { engines?: { node?: string } };

    const declared = Number(/(\d+)/.exec(pkg.engines.node)![1]);
    const required = Number(/(\d+)/.exec(sdk.engines?.node ?? '0')![1]);
    expect(declared).toBeGreaterThanOrEqual(required);
  });
});

/** Every workflow this package ships. */
const ALL_WORKFLOWS = [
  GENERATOR_WORKFLOW_FILE,
  ADJUDICATION_WORKFLOW_FILE,
  RESEARCH_WORKFLOW_FILE,
] as const;

describe('packaged asset paths', () => {
  it('resolves every workflow to an absolute path that exists', () => {
    for (const file of ALL_WORKFLOWS) {
      const path = packageWorkflowPath(file);
      expect(isAbsolute(path)).toBe(true);
      expect(existsSync(path)).toBe(true);
    }
  });

  it('ships the prompt-library directory inside the package', () => {
    expect(packagePromptDir()).toBe(join(packageRoot(), 'workflows', 'prompts'));
    expect(shippedPromptNames().length).toBeGreaterThan(0);
  });
});

describe('workflow capability boundary', () => {
  it('the adjudication workflow is a single agent node with an EMPTY tool allowlist', () => {
    const doc = loadWorkflow(ADJUDICATION_WORKFLOW_FILE);
    const specs = agentSpecs(doc);

    expect(specs).toHaveLength(1);
    expect(specs[0]?.id).toBe('adjudicate');
    // `tools: []` is the enforced grant. Present, and empty — not absent
    // (absent would inherit the parent agent's toolset) and not a deny-list
    // (Hermes does not enforce spec.deny_tools).
    expect(Array.isArray(specs[0]?.spec.tools)).toBe(true);
    expect(specs[0]?.spec.tools).toEqual([]);
    expect(specs[0]?.spec).not.toHaveProperty('deny_tools');
  });

  it('no node in any shipped workflow can reach shell escape hatch abuse or a sub-agent', () => {
    // patch/delegate/execute_code stay banned everywhere.
    // write_file is allowed on the morning generator for workspace artifacts only.
    // terminal is allowed on generator scout + debate stages for pm-desk helper CLIs.
    const forbidden = [
      'close_terminal',
      'patch',
      'delegate_task',
      'execute_code',
    ];
    const generatorResearchIds = (id: string) =>
      id === 'dd' ||
      id === 'dq' ||
      id === 'directive.branch' ||
      id.startsWith('directive') ||
      id.startsWith('eval_debate.');
    const generatorWriteIds = (id: string) =>
      generatorResearchIds(id) || id === 'prepare' || id === 'plan';
    const violations: string[] = [];

    for (const file of ALL_WORKFLOWS) {
      for (const { id, spec } of agentSpecs(loadWorkflow(file))) {
        expect(Array.isArray(spec.tools), `${file}:${id} declares no tools list`).toBe(true);
        for (const tool of spec.tools ?? []) {
          if (forbidden.includes(tool)) violations.push(`${file}:${id}: ${tool}`);
        }
        if ((spec.tools ?? []).includes('write_file')) {
          const ok = file === GENERATOR_WORKFLOW_FILE && generatorWriteIds(id);
          if (!ok) violations.push(`${file}:${id}: write_file (not allowed here)`);
        }
        if ((spec.tools ?? []).includes('terminal')) {
          const ok = file === GENERATOR_WORKFLOW_FILE && generatorResearchIds(id);
          if (!ok) violations.push(`${file}:${id}: terminal (not allowed here)`);
        }
      }
    }
    expect(violations).toEqual([]);
  });

  it('keeps X and expensive browser recovery out of the primary morning pass', () => {
    const browser = ['browser_navigate', 'browser_snapshot', 'browser_click', 'browser_scroll'];
    const withBrowser: string[] = [];
    const withX: string[] = [];
    const withTerminal: string[] = [];

    for (const file of ALL_WORKFLOWS) {
      for (const { id, spec } of agentSpecs(loadWorkflow(file))) {
        const tools = spec.tools ?? [];
        if (tools.some((t) => browser.includes(t))) withBrowser.push(`${file}:${id}`);
        if (tools.includes('x_search')) withX.push(`${file}:${id}`);
        if (tools.includes('terminal')) withTerminal.push(`${file}:${id}`);
      }
    }
    const gen = GENERATOR_WORKFLOW_FILE;
    // Only DD may recover a blocked primary source; scouting and council are
    // bounded to web results plus persisted artifacts.
    expect(withBrowser).toEqual([`${gen}:dd`]);
    expect(withTerminal).toEqual([`${gen}:dd`]);
    expect(withX).toEqual([]);
  });

  it('uses a bounded two-person vote; plan is workspace read/write only', () => {
    const doc = loadWorkflow(GENERATOR_WORKFLOW_FILE);
    const specs = agentSpecs(doc);
    const byId = new Map(specs.map((s) => [s.id, s]));
    const debateAgents = specs.filter((s) => s.id.startsWith('eval_debate.'));
    expect(debateAgents.map(({ id }) => id)).toEqual([
      'eval_debate.participant.bull',
      'eval_debate.participant.bear',
    ]);
    for (const { id, spec } of debateAgents) {
      expect(spec.tools, `${id} must read/write evidence`).toEqual(
        expect.arrayContaining(['web_search', 'web_extract', 'read_file', 'write_file']),
      );
      expect(spec.tools).not.toEqual(expect.arrayContaining(['x_search', 'terminal', 'browser_navigate']));
    }
    const debate = (doc.nodes as (DebateishNode & {
      protocol?: string;
      max_rounds?: number;
    })[]).find((node) => node.id === 'eval_debate');
    expect(debate?.protocol).toBe('vote');
    expect(debate?.max_rounds).toBe(1);
    expect(debate?.directive?.judge).toBeUndefined();
    const planTools = byId.get('plan')?.spec.tools ?? [];
    expect(planTools).toEqual(expect.arrayContaining(['read_file', 'write_file']));
    expect(planTools).not.toEqual(expect.arrayContaining(['terminal', 'web_search', 'browser_navigate']));
    const prepareTools = byId.get('prepare')?.spec.tools ?? [];
    expect(prepareTools).toEqual(expect.arrayContaining(['read_file', 'write_file', 'session_search']));
  });

  it('generator pins the persistent pm-desk workspace', () => {
    const doc = loadWorkflow(GENERATOR_WORKFLOW_FILE);
    expect(doc.workspace).toBe('pm-desk');
  });

  it('caps the primary morning run and keeps premium escalation out of the graph', () => {
    const raw = readFileSync(packageWorkflowPath(GENERATOR_WORKFLOW_FILE), 'utf8');
    const doc = parseYaml(raw) as {
      max_budget_usd?: number;
      nodes: { id: string; kind: string; spec?: { model?: string; provider?: string; max_turns?: number; budget_usd?: number }; participants?: { spec?: { model?: string; provider?: string; max_turns?: number; budget_usd?: number } }[]; directive?: { judge?: unknown; judge_model?: string } }[];
    };
    expect(doc.max_budget_usd).toBe(3);
    const models = new Set<string>();
    const providers = new Set<string>();
    for (const n of doc.nodes) {
      if (n.spec?.model) models.add(n.spec.model);
      if (n.spec?.provider) providers.add(n.spec.provider);
      for (const p of n.participants ?? []) {
        if (p.spec?.model) models.add(p.spec.model);
        if (p.spec?.provider) providers.add(p.spec.provider);
      }
      expect(n.directive?.judge).toBeUndefined();
      expect(n.directive?.judge_model).toBeUndefined();
    }
    for (const { id, spec } of agentSpecs(loadWorkflow(GENERATOR_WORKFLOW_FILE))) {
      expect(spec, id).not.toHaveProperty('max_turns');
      expect(spec, id).not.toHaveProperty('budget_usd');
    }
    expect(models).toEqual(new Set(['z-ai/glm-5.2', 'moonshotai/kimi-k2.5', 'openai/gpt-5.4-mini']));
    expect(providers).toEqual(new Set(['openrouter']));
    expect(raw).not.toMatch(/grok-4\.5|xai-oauth|gpt-5\.6-terra|claude-sonnet-5/);
  });

  it('the generator ends at a dual-control telegram gate', () => {
    const doc = loadWorkflow(GENERATOR_WORKFLOW_FILE);
    const gate = doc.nodes.find((n) => n.kind === 'gate');
    expect(gate?.id).toBe('paper_gate');
    const raw = readFileSync(packageWorkflowPath(GENERATOR_WORKFLOW_FILE), 'utf8');
    const parsed = parseYaml(raw) as { gates?: Record<string, Record<string, unknown>> };
    expect(parsed.gates?.['paper_gate']).toMatchObject({
      channel: 'telegram',
      dual_control: true,
      on_timeout: 'shelve',
    });
  });

  it('the generator never conditions an edge on an agent node’s output', () => {
    // An agent output is the envelope {text, node, result, ...}: the model's
    // JSON is a string inside `text`, never parsed into the output. So
    // `$.decision == 'advance'` raises TemplateError, which the driver fails
    // closed to "blocked" — silently skipping everything downstream. Same for
    // `over:` against a model-produced list, which fails the node outright.
    const raw = readFileSync(packageWorkflowPath(GENERATOR_WORKFLOW_FILE), 'utf8');
    const doc = parseYaml(raw) as {
      nodes: { id: string; kind: string; over?: string }[];
      edges: { from: string; to: string; condition?: string }[];
    };
    const agentIds = new Set(doc.nodes.filter((n) => n.kind === 'agent').map((n) => n.id));

    for (const edge of doc.edges) {
      if (edge.condition !== undefined) {
        expect(
          agentIds.has(edge.from),
          `edge ${edge.from}->${edge.to} conditions on an agent`,
        ).toBe(false);
      }
    }
    for (const node of doc.nodes) {
      if (node.over === undefined) continue;
      const head = /\{\{\s*([A-Za-z_][A-Za-z0-9_]*)/.exec(node.over)?.[1];
      expect(agentIds.has(String(head)), `${node.id} fans out over an agent output`).toBe(false);
    }
  });

  it('every `library:` a workflow references is shipped in this package', () => {
    const shipped = new Set(shippedPromptNames());
    const referenced: string[] = [];

    for (const file of ALL_WORKFLOWS) {
      for (const { spec } of agentSpecs(loadWorkflow(file))) {
        const prompt = spec.prompt;
        if (prompt && typeof prompt === 'object' && typeof prompt.library === 'string') {
          referenced.push(prompt.library);
        }
      }
    }

    // Guards against the failure this package actually hit: the libraries
    // existed only in one operator's ~/.hermes, so the research spine could not
    // validate anywhere else.
    expect(referenced.length).toBeGreaterThan(0);
    expect(referenced.filter((name) => !shipped.has(name))).toEqual([]);
  });

  it('the adjudication prompt is rendered by the launcher, not fetched by the agent', () => {
    const doc = loadWorkflow(ADJUDICATION_WORKFLOW_FILE);
    const prompt = agentSpecs(doc)[0]?.spec.prompt;
    expect(typeof prompt).toBe('string');
    expect(String(prompt)).toContain('{{ input.prompt }}');
  });
});

describe('thesis reopen workflow', () => {
  // The reopen workflow is NOT in ALL_WORKFLOWS on purpose: the capability-
  // boundary tests above express the generator's grants (write_file/terminal
  // only on scout+plan), while the reopen graph grants write_file to its
  // context/research/plan nodes and terminal to research — the same shape as a
  // generator scout+plan, just for one thesis. It gets its own boundary check.

  it('is a straight line context -> research -> plan -> paper_gate on the pm-desk workspace', () => {
    const doc = loadWorkflow(REOPEN_WORKFLOW_FILE);
    expect(doc.workspace).toBe('pm-desk');
    const ids = doc.nodes.map((n) => n.id);
    expect(ids).toEqual(['context', 'research', 'plan', 'paper_gate']);
    const edges = (parseYaml(readFileSync(packageWorkflowPath(REOPEN_WORKFLOW_FILE), 'utf8')) as {
      edges: { from: string; to: string }[];
    }).edges;
    expect(edges).toEqual([
      { from: 'context', to: 'research' },
      { from: 'research', to: 'plan' },
      { from: 'plan', to: 'paper_gate' },
    ]);
  });

  it('the plan node uses a STRICT shipped library — no freestyle reopen schema', () => {
    // C3: the bespoke host reopen freestyled its own inline schema and the gate
    // opened on an invalid plan. The productized reopen uses a shipped library
    // prompt (pm-reopen-plan-v1) that mirrors the morning's strict contract but
    // references the reopen's nodes — the morning library cannot be reused
    // verbatim because Hermes validates template node references.
    const doc = loadWorkflow(REOPEN_WORKFLOW_FILE);
    const plan = doc.nodes.find((n) => n.id === 'plan');
    const prompt = plan?.spec?.prompt;
    expect(prompt && typeof prompt === 'object' && prompt.library).toBe('pm-reopen-plan-v1');
  });

  it('context and research use the dedicated reopen libraries', () => {
    const doc = loadWorkflow(REOPEN_WORKFLOW_FILE);
    const lib = (id: string) => {
      const node = doc.nodes.find((n) => n.id === id);
      const p = node?.spec?.prompt;
      return p && typeof p === 'object' && typeof p.library === 'string' ? p.library : undefined;
    };
    expect(lib('context')).toBe('pm-reopen-context-v1');
    expect(lib('research')).toBe('pm-reopen-research-v1');
  });

  it('every library the reopen workflow references ships in this package', () => {
    const shipped = new Set(shippedPromptNames());
    const referenced: string[] = [];
    for (const { spec } of agentSpecs(loadWorkflow(REOPEN_WORKFLOW_FILE))) {
      const prompt = spec.prompt;
      if (prompt && typeof prompt === 'object' && typeof prompt.library === 'string') {
        referenced.push(prompt.library);
      }
    }
    expect(referenced).toEqual([
      'pm-reopen-context-v1',
      'pm-reopen-research-v1',
      'pm-reopen-plan-v1',
    ]);
    expect(referenced.filter((name) => !shipped.has(name))).toEqual([]);
  });

  it('grants no shell escape hatch; write_file only on context/research/plan, terminal only on research', () => {
    const forbidden = ['close_terminal', 'patch', 'delegate_task', 'execute_code'];
    const violations: string[] = [];
    for (const { id, spec } of agentSpecs(loadWorkflow(REOPEN_WORKFLOW_FILE))) {
      expect(Array.isArray(spec.tools), `${id} declares no tools list`).toBe(true);
      for (const tool of spec.tools ?? []) {
        if (forbidden.includes(tool)) violations.push(`${id}: ${tool}`);
      }
      if ((spec.tools ?? []).includes('write_file')) {
        if (!['context', 'research', 'plan'].includes(id)) violations.push(`${id}: write_file`);
      }
      if ((spec.tools ?? []).includes('terminal')) {
        if (id !== 'research') violations.push(`${id}: terminal`);
      }
    }
    expect(violations).toEqual([]);
  });

  it('ends at a dual-control telegram gate', () => {
    const doc = loadWorkflow(REOPEN_WORKFLOW_FILE);
    expect(doc.nodes.find((n) => n.kind === 'gate')?.id).toBe('paper_gate');
    const parsed = parseYaml(readFileSync(packageWorkflowPath(REOPEN_WORKFLOW_FILE), 'utf8')) as {
      gates?: Record<string, Record<string, unknown>>;
    };
    expect(parsed.gates?.['paper_gate']).toMatchObject({
      channel: 'telegram',
      dual_control: true,
      on_timeout: 'shelve',
    });
  });
});

describe('prompt-library installer', () => {
  let home: string;

  beforeEach(() => {
    home = mkdtempSync(join(tmpdir(), 'pm-desk-hermes-home-'));
  });

  afterEach(() => {
    rmSync(home, { recursive: true, force: true });
  });

  it('resolves the target home from the explicit path, then $HERMES_HOME, then ~/.hermes', () => {
    expect(resolveHermesHome('/tmp/explicit', { HERMES_HOME: '/tmp/env' })).toBe('/tmp/explicit');
    expect(resolveHermesHome(undefined, { HERMES_HOME: '/tmp/env' })).toBe('/tmp/env');
    expect(resolveHermesHome(undefined, {})).toMatch(/\.hermes$/);
    // An empty env var must not resolve to an empty path.
    expect(resolveHermesHome(undefined, { HERMES_HOME: '' })).toMatch(/\.hermes$/);
  });

  it('plans an install without writing anything', () => {
    const plan = planPromptInstall({ hermesHome: home });

    expect(plan.target_dir).toBe(join(home, 'workflows', 'prompts'));
    expect(plan.entries.length).toBe(shippedPromptNames().length);
    expect(plan.entries.every((e) => e.action === 'install')).toBe(true);
    expect(plan.blocked).toBe(false);
    // The whole point of a dry run: the directory does not even exist yet.
    expect(existsSync(plan.target_dir)).toBe(false);
  });

  it('applies the plan into a temp home and is idempotent on a second run', () => {
    const written = applyPromptInstall(planPromptInstall({ hermesHome: home }));
    expect(written.length).toBe(shippedPromptNames().length);

    for (const name of shippedPromptNames()) {
      const installed = readFileSync(join(home, 'workflows', 'prompts', `${name}.yaml`), 'utf8');
      const packaged = readFileSync(join(packagePromptDir(), `${name}.yaml`), 'utf8');
      expect(installed).toBe(packaged);
      // Hermes requires a non-empty string `prompt:` field in every library.
      const doc = parseYaml(installed) as { name: string; prompt: string };
      expect(doc.name).toBe(name);
      expect(typeof doc.prompt).toBe('string');
      expect(doc.prompt.trim().length).toBeGreaterThan(0);
    }

    const second = planPromptInstall({ hermesHome: home });
    expect(second.entries.every((e) => e.action === 'up_to_date')).toBe(true);
    expect(applyPromptInstall(second)).toEqual([]);
  });

  it('refuses to clobber a locally modified library unless forced', () => {
    const name = shippedPromptNames()[0]!;
    const target = join(home, 'workflows', 'prompts', `${name}.yaml`);
    mkdirSync(join(home, 'workflows', 'prompts'), { recursive: true });
    writeFileSync(target, 'name: hand-edited\nprompt: |\n  local changes\n', 'utf8');

    const plan = planPromptInstall({ hermesHome: home });
    expect(plan.blocked).toBe(true);
    expect(plan.entries.find((e) => e.name === name)?.action).toBe('conflict');
    expect(() => applyPromptInstall(plan)).toThrow(ConfigError);
    // Still untouched.
    expect(readFileSync(target, 'utf8')).toContain('hand-edited');

    const forced = planPromptInstall({ hermesHome: home, force: true });
    expect(forced.blocked).toBe(false);
    expect(forced.entries.find((e) => e.name === name)?.action).toBe('overwrite');
    applyPromptInstall(forced);
    expect(readFileSync(target, 'utf8')).toBe(
      readFileSync(join(packagePromptDir(), `${name}.yaml`), 'utf8'),
    );
  });

  it('never targets the real ~/.hermes when a home is passed explicitly', () => {
    const plan = planPromptInstall({
      hermesHome: home,
      env: { HERMES_HOME: '/should/be/ignored' },
    });
    expect(plan.target_dir.startsWith(home)).toBe(true);
  });
});

describe('opt-in Hermes launcher', () => {
  let deskHome: string;
  let store: ReturnType<typeof openStore>;

  const ENVELOPE = {
    version: 1,
    signal_id: 'sig_' + '3'.repeat(32),
    kind: 'primary_source_change',
    severity: 'high',
    observed_at: '2026-07-30T12:00:00.000Z',
    rule_id: 'gdp_release_change',
    rule_version: '1',
    market_refs: [],
    source_refs: [
      {
        source_id: 'example_official_release',
        url: 'https://example.gov/release',
        previous_hash: 'a'.repeat(64),
        current_hash: 'b'.repeat(64),
        artifact_ref: 'sha256:' + 'b'.repeat(64),
      },
    ],
    evidence: { claims: ['Q1 GDP revised 3.1% -> 2.4%'] },
    paper_only: true,
    dedupe_key: 'gdp_release_change:1:' + 'b'.repeat(64),
  };

  beforeEach(() => {
    deskHome = mkdtempSync(join(tmpdir(), 'pm-desk-launcher-'));
    store = openStore({ home: deskHome });
  });

  afterEach(() => {
    store.close();
    rmSync(deskHome, { recursive: true, force: true });
  });

  it('defaults to the packaged workflow by absolute path, not a cwd-relative one', async () => {
    const calls: string[][] = [];
    const dispatcher = new HermesLauncherDispatcher(store, {
      enabled: true,
      runner: async (cmd, args) => {
        calls.push([cmd, ...args]);
        return { code: 0, stdout: '', stderr: '' };
      },
    });

    store.signals.record(ENVELOPE as never, 'test');
    await dispatcher.dispatch(ENVELOPE as never);

    // argv is [binary, 'workflow', 'run', <path>, '--input', <json>].
    const path = calls[0]?.[3];
    expect(path).toBe(packageWorkflowPath(ADJUDICATION_WORKFLOW_FILE));
    expect(isAbsolute(String(path))).toBe(true);
    expect(existsSync(String(path))).toBe(true);
  });

  it('appends --dry-run only when explicitly asked, so a real run is never implicit', async () => {
    const calls: string[][] = [];
    const runner = async (cmd: string, args: string[]) => {
      calls.push([cmd, ...args]);
      return { code: 0, stdout: '', stderr: '' };
    };

    store.signals.record(ENVELOPE as never, 'test');
    await new HermesLauncherDispatcher(store, { enabled: true, runner }).dispatch(
      ENVELOPE as never,
    );
    expect(calls[0]).not.toContain('--dry-run');

    await new HermesLauncherDispatcher(store, { enabled: true, dryRun: true, runner }).dispatch(
      ENVELOPE as never,
    );
    expect(calls[1]).toContain('--dry-run');
  });
});

/**
 * The real thing: run the installed Hermes CLI against a throwaway HERMES_HOME
 * and prove (a) both workflows validate, (b) the research spine validates ONLY
 * after the prompt libraries are installed, and (c) the launcher's argv is
 * accepted by `hermes workflow run --dry-run`.
 *
 * Opt-in: Hermes is a Python install, absent from this package's Node-only CI.
 * Run it with:  PM_DESK_HERMES_INTEGRATION=1 npm test
 */
const integration = process.env.PM_DESK_HERMES_INTEGRATION === '1' ? describe : describe.skip;

integration('real Hermes CLI (opt-in)', () => {
  let home: string;

  const hermes = (args: string[], env: Record<string, string> = {}) => {
    try {
      const stdout = execFileSync(process.env.PM_DESK_HERMES_BIN ?? 'hermes', args, {
        encoding: 'utf8',
        env: { ...process.env, HERMES_HOME: home, ...env },
        stdio: ['ignore', 'pipe', 'pipe'],
      });
      return { code: 0, stdout, stderr: '' };
    } catch (err) {
      const e = err as { status?: number; stdout?: string; stderr?: string };
      return { code: e.status ?? 1, stdout: e.stdout ?? '', stderr: e.stderr ?? '' };
    }
  };

  beforeEach(() => {
    home = mkdtempSync(join(tmpdir(), 'pm-desk-hermes-integration-'));
  });

  afterEach(() => {
    rmSync(home, { recursive: true, force: true });
  });

  it('validates the adjudication workflow with no prompt libraries installed', () => {
    const result = hermes([
      'workflow',
      'validate',
      packageWorkflowPath(ADJUDICATION_WORKFLOW_FILE),
    ]);
    expect(result.code, result.stderr).toBe(0);
    expect(result.stdout).toContain('pm_signal_adjudication_v0');
  });

  it('rejects the research spine until its prompt libraries are installed, then accepts it', () => {
    const before = hermes(['workflow', 'validate', packageWorkflowPath(RESEARCH_WORKFLOW_FILE)]);
    expect(before.code).not.toBe(0);
    expect(`${before.stdout}${before.stderr}`).toContain('PROMPT_LIBRARY');

    applyPromptInstall(planPromptInstall({ hermesHome: home }));

    const after = hermes(['workflow', 'validate', packageWorkflowPath(RESEARCH_WORKFLOW_FILE)]);
    expect(after.code, after.stderr).toBe(0);
    expect(after.stdout).toContain('pm_desk_paper_v0');
  });

  it('accepts the exact argv the launcher builds (compile + plan only, no agent)', () => {
    const input = JSON.stringify({
      signal_id: 'sig_integration',
      prompt: 'PAPER ONLY — rendered locally by pm-desk.',
      paper_only: true,
    });
    const result = hermes([
      'workflow',
      'run',
      packageWorkflowPath(ADJUDICATION_WORKFLOW_FILE),
      '--input',
      input,
      '--dry-run',
    ]);
    expect(result.code, `${result.stdout}${result.stderr}`).toBe(0);
    // --dry-run compiles and plans the ready set; it must never spawn an agent.
    expect(`${result.stdout}`.toLowerCase()).toContain('adjudicate');
  });

  it('rejects the morning generator until its prompt libraries are installed, then accepts it', () => {
    const before = hermes(['workflow', 'validate', packageWorkflowPath(GENERATOR_WORKFLOW_FILE)]);
    expect(before.code).not.toBe(0);
    expect(`${before.stdout}${before.stderr}`).toContain('PROMPT_LIBRARY');

    applyPromptInstall(planPromptInstall({ hermesHome: home }));

    const after = hermes(['workflow', 'validate', packageWorkflowPath(GENERATOR_WORKFLOW_FILE)]);
    expect(after.code, `${after.stdout}${after.stderr}`).toBe(0);
    expect(after.stdout).toContain('pm_morning_generator_v0');
  });

  it('accepts the generator’s research tool grant — the names really exist on this host', () => {
    // The failure this guards: `spec.tools` entries are checked against the
    // live toolset registry (code TOOLS_UNKNOWN), so a plausible-looking but
    // unregistered name like `browser_extract` fails validation. This is the
    // only way to know the DD grant is real rather than aspirational.
    applyPromptInstall(planPromptInstall({ hermesHome: home }));
    const result = hermes(['workflow', 'validate', packageWorkflowPath(GENERATOR_WORKFLOW_FILE)]);
    expect(result.code, `${result.stdout}${result.stderr}`).toBe(0);
    expect(`${result.stdout}${result.stderr}`).not.toContain('TOOLS_UNKNOWN');
  });

  it('plans the generator’s first node from a real compiled seed, spawning no agent', () => {
    applyPromptInstall(planPromptInstall({ hermesHome: home }));
    const input = JSON.stringify({
      directive_cards: [{ card_id: 'unfamiliar_eligible_cluster', family_id: 'explore_seed' }],
      seed: { seed_id: 'seed_integration', run_date: '2026-07-31', taxonomy_version: 1 },
    });
    const result = hermes([
      'workflow',
      'run',
      packageWorkflowPath(GENERATOR_WORKFLOW_FILE),
      '--input',
      input,
      '--dry-run',
    ]);
    expect(result.code, `${result.stdout}${result.stderr}`).toBe(0);
    expect(result.stdout).toContain('prepare');
    expect(result.stdout).toContain('dry_run');
  });
});
