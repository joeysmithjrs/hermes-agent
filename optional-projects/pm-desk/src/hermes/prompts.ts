/**
 * Installer for the workflow prompt libraries this desk's research spine needs.
 *
 * `pm-desk-paper-v0.yaml` uses the Hermes `spec.prompt: {library: <name>}` form.
 * Hermes resolves those names against, in order:
 *
 *   1. $HERMES_HOME/workflows/prompts/<name>.yaml   (operator-owned)
 *   2. <hermes-repo>/workflow/prompts/builtin/*     (shipped with Hermes)
 *
 * PM Desk is an optional, edge-owned package, so its libraries belong in the
 * operator-owned directory — NOT in Hermes' builtin tree. Until they are
 * installed there, `hermes workflow validate` hard-rejects the research spine
 * with `PROMPT_LIBRARY` (there is no not-found -> empty-prompt fall-through).
 *
 * Installing is therefore an explicit, opt-in operator action, and this module
 * is the whole of it:
 *
 *   - `planPromptInstall()` computes what WOULD change and writes nothing.
 *   - `applyPromptInstall()` is the only function here that touches the disk.
 *
 * The CLI defaults to the plan. Nothing on an ordinary `npm test` / `npm run
 * build` path calls the apply function, so no PM Desk workflow ever mutates a
 * real $HERMES_HOME on its own.
 */

import { mkdirSync, readdirSync, readFileSync, writeFileSync } from 'node:fs';
import { homedir } from 'node:os';
import { isAbsolute, join, resolve } from 'node:path';

import { ConfigError } from '../core/errors.js';
import { sha256Hex } from '../core/hash.js';
import { packagePromptDir } from './package-paths.js';

export { packagePromptDir };

/** What installing one library would do (or did). */
export type PromptInstallAction =
  | 'install' // not present in $HERMES_HOME yet
  | 'up_to_date' // present and byte-identical
  | 'overwrite' // present, differs, and --force was given
  | 'conflict'; // present, differs, no --force — refused

export interface PromptInstallEntry {
  name: string;
  source: string;
  target: string;
  action: PromptInstallAction;
  source_sha256: string;
  target_sha256?: string;
}

export interface PromptInstallPlan {
  hermes_home: string;
  target_dir: string;
  package_dir: string;
  force: boolean;
  entries: PromptInstallEntry[];
  /** True when at least one entry is a `conflict`; apply refuses in that case. */
  blocked: boolean;
}

export interface PlanOptions {
  /** Explicit target home. Falls back to $HERMES_HOME, then ~/.hermes. */
  hermesHome?: string;
  force?: boolean;
  /** Overridable for tests; defaults to the libraries shipped in this package. */
  packageDir?: string;
  env?: Record<string, string | undefined>;
}

/**
 * Resolve the Hermes home the same way Hermes' own `get_hermes_home()` does for
 * the default profile: an explicit path wins, then $HERMES_HOME, then ~/.hermes.
 *
 * Deliberately NOT read from Hermes' config.yaml: parsing another project's
 * config to guess a profile directory is exactly the kind of coupling that
 * breaks silently. An operator on a non-default profile passes --hermes-home.
 */
export function resolveHermesHome(
  explicit?: string,
  env: Record<string, string | undefined> = process.env,
): string {
  const candidate = explicit ?? env.HERMES_HOME;
  if (candidate && candidate.trim() !== '') {
    return isAbsolute(candidate) ? candidate : resolve(candidate);
  }
  return join(homedir(), '.hermes');
}

/** The library names this package ships, sorted for deterministic output. */
export function shippedPromptNames(packageDir: string = packagePromptDir()): string[] {
  let files: string[];
  try {
    files = readdirSync(packageDir);
  } catch (cause) {
    throw new ConfigError(`cannot read the packaged prompt library directory: ${packageDir}`, {
      hint: 'This directory ships with PM Desk. If it is missing, the checkout is incomplete.',
      cause,
    });
  }
  return files
    .filter((f) => f.endsWith('.yaml'))
    .map((f) => f.slice(0, -'.yaml'.length))
    .sort();
}

export function planPromptInstall(options: PlanOptions = {}): PromptInstallPlan {
  const packageDir = options.packageDir ?? packagePromptDir();
  const hermesHome = resolveHermesHome(options.hermesHome, options.env);
  const targetDir = join(hermesHome, 'workflows', 'prompts');
  const force = options.force ?? false;

  const entries: PromptInstallEntry[] = shippedPromptNames(packageDir).map((name) => {
    const source = join(packageDir, `${name}.yaml`);
    const target = join(targetDir, `${name}.yaml`);
    const sourceBody = readFileSync(source, 'utf8');
    const sourceSha = sha256Hex(sourceBody);

    let targetBody: string | undefined;
    try {
      targetBody = readFileSync(target, 'utf8');
    } catch {
      targetBody = undefined;
    }

    let action: PromptInstallAction;
    if (targetBody === undefined) action = 'install';
    else if (sha256Hex(targetBody) === sourceSha) action = 'up_to_date';
    else action = force ? 'overwrite' : 'conflict';

    return {
      name,
      source,
      target,
      action,
      source_sha256: sourceSha,
      ...(targetBody === undefined ? {} : { target_sha256: sha256Hex(targetBody) }),
    };
  });

  return {
    hermes_home: hermesHome,
    target_dir: targetDir,
    package_dir: packageDir,
    force,
    entries,
    blocked: entries.some((e) => e.action === 'conflict'),
  };
}

/**
 * Write the plan. Refuses outright if any entry conflicts, so a hand-edited
 * library in $HERMES_HOME is never silently clobbered.
 */
export function applyPromptInstall(plan: PromptInstallPlan): PromptInstallEntry[] {
  if (plan.blocked) {
    const names = plan.entries.filter((e) => e.action === 'conflict').map((e) => e.name);
    throw new ConfigError(
      `refusing to overwrite locally modified prompt librar${names.length > 1 ? 'ies' : 'y'}: ${names.join(', ')}`,
      {
        hint: 'Diff them against workflows/prompts/ in this package, then re-run with --force to take the packaged version.',
      },
    );
  }

  mkdirSync(plan.target_dir, { recursive: true });
  const written: PromptInstallEntry[] = [];
  for (const entry of plan.entries) {
    if (entry.action === 'up_to_date') continue;
    writeFileSync(entry.target, readFileSync(entry.source, 'utf8'), 'utf8');
    written.push(entry);
  }
  return written;
}
