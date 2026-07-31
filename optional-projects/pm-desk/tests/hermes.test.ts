import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { isAbsolute, join } from 'node:path';

import { parse as parseYaml } from 'yaml';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { ConfigError } from '../src/core/errors.js';
import {
  ADJUDICATION_WORKFLOW_FILE,
  packagePromptDir,
  packageRoot,
  packageWorkflowPath,
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

/** Every agent-bearing spec in a workflow, including fanout branch specs. */
function agentSpecs(doc: WorkflowDoc): { id: string; spec: NodeSpec }[] {
  const out: { id: string; spec: NodeSpec }[] = [];
  for (const node of doc.nodes) {
    if ((node.kind === 'agent' || node.kind === 'supervisor') && node.spec) {
      out.push({ id: node.id, spec: node.spec });
    }
    if (node.kind === 'fanout' && node.branch?.kind === 'agent' && node.branch.spec) {
      out.push({ id: `${node.id}.branch`, spec: node.branch.spec });
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

describe('packaged asset paths', () => {
  it('resolves both workflows to absolute paths that exist', () => {
    for (const file of [ADJUDICATION_WORKFLOW_FILE, RESEARCH_WORKFLOW_FILE]) {
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

  it('no node anywhere in either workflow can reach a terminal, browser or network tool', () => {
    const forbidden = [
      'terminal',
      'browser_navigate',
      'browser_click',
      'browser_type',
      'web_search',
      'web_extract',
      'write_file',
      'patch',
      'delegate_task',
    ];
    const violations: string[] = [];

    for (const file of [ADJUDICATION_WORKFLOW_FILE, RESEARCH_WORKFLOW_FILE]) {
      for (const { id, spec } of agentSpecs(loadWorkflow(file))) {
        // An agent node without an explicit `tools:` would inherit whatever the
        // parent agent has. Every node must state its grant.
        expect(Array.isArray(spec.tools), `${file}:${id} declares no tools list`).toBe(true);
        for (const tool of spec.tools ?? []) {
          if (forbidden.includes(tool)) violations.push(`${file}:${id}: ${tool}`);
        }
      }
    }
    expect(violations).toEqual([]);
  });

  it('every `library:` a workflow references is shipped in this package', () => {
    const shipped = new Set(shippedPromptNames());
    const referenced: string[] = [];

    for (const file of [ADJUDICATION_WORKFLOW_FILE, RESEARCH_WORKFLOW_FILE]) {
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
    const plan = planPromptInstall({ hermesHome: home, env: { HERMES_HOME: '/should/be/ignored' } });
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
    await new HermesLauncherDispatcher(store, { enabled: true, runner }).dispatch(ENVELOPE as never);
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
    const result = hermes(['workflow', 'validate', packageWorkflowPath(ADJUDICATION_WORKFLOW_FILE)]);
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
});
