/**
 * Absolute paths to assets that ship inside this package.
 *
 * The desk is a CLI: an operator runs `pm-desk ingress serve` from wherever
 * they happen to be. Anything that resolved a packaged asset relative to the
 * process cwd would work in the repo and fail everywhere else, so every
 * reference to a shipped workflow or prompt library goes through here.
 */

import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * The package root. This file lives at `src/hermes/` under tsx and at
 * `dist/hermes/` after `npm run build`; both are exactly two levels below the
 * package root, so one expression covers both.
 */
export function packageRoot(): string {
  return resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');
}

/** Absolute path to a workflow YAML shipped in `workflows/`. */
export function packageWorkflowPath(file: string): string {
  return join(packageRoot(), 'workflows', file);
}

/** Absolute path to the prompt-library directory shipped in `workflows/prompts/`. */
export function packagePromptDir(): string {
  return join(packageRoot(), 'workflows', 'prompts');
}

/** The adjudication workflow the opt-in Hermes launcher runs by default. */
export const ADJUDICATION_WORKFLOW_FILE = 'pm-signal-adjudication-v0.yaml';

/** The broad, operator-initiated research spine. */
export const RESEARCH_WORKFLOW_FILE = 'pm-desk-paper-v0.yaml';
