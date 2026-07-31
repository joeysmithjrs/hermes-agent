#!/usr/bin/env node
/**
 * Fail fast, and say exactly why, when the host Node is too old for this package.
 *
 * PM Desk requires Node >= 24 because its hardest dependency does:
 * `@polymarket/client` declares `engines.node: ">=24"` in EVERY published
 * version (checked across 0.1.0-beta.*, 0.1.0, 0.2.0 and 0.3.0-beta.0), so
 * there is no older release to pin that supports Node 22. The offline test
 * suite happens to pass on Node 22 — it exercises the SDK through a fake — but
 * that is not support, and this package does not claim it.
 *
 * `.npmrc` sets `engine-strict=true`, so `npm install` already refuses on an
 * unsupported Node. This script covers the case where a checkout was installed
 * elsewhere (or `--engine-strict=false` was passed) and the mismatch would
 * otherwise surface as a confusing runtime error deep inside the SDK.
 *
 * The pure functions are exported so the test suite can assert the parsing,
 * which is easy to get subtly wrong: `">=24.0.0"` starts with a non-digit.
 */

import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

/**
 * Lowest major version an `engines.node` range admits.
 * Returns null when the range has no leading numeric floor we can read.
 */
export function parseRequiredMajor(range) {
  const match = /(\d+)/.exec(String(range ?? ''));
  return match ? Number(match[1]) : null;
}

/** @returns {{ok: boolean, requiredMajor: number|null, actualMajor: number}} */
export function evaluate(nodeVersion, range) {
  const requiredMajor = parseRequiredMajor(range);
  const actualMajor = Number(String(nodeVersion).replace(/^v/, '').split('.')[0]);
  return {
    ok: requiredMajor !== null && actualMajor >= requiredMajor,
    requiredMajor,
    actualMajor,
  };
}

export function failureMessage(nodeVersion, requiredMajor) {
  return [
    `PM Desk requires Node >= ${requiredMajor}. This is Node ${nodeVersion}.`,
    '',
    'Reason: @polymarket/client declares engines.node ">=24" in every published',
    'version, so there is no compatible older release to pin. Running the desk on',
    'an older Node is unsupported, not merely unwarned.',
    '',
    'Install Node 24 (nvm: `nvm install 24 && nvm use 24`), then re-run:',
    '  npm ci && npm run check',
  ].join('\n');
}

function main() {
  const packageRoot = join(dirname(fileURLToPath(import.meta.url)), '..');
  const pkg = JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf8'));
  const range = pkg.engines?.node;
  const { ok, requiredMajor } = evaluate(process.versions.node, range);

  if (requiredMajor === null) {
    console.error(`preflight: cannot read a major version from engines.node (${range})`);
    process.exit(2);
  }
  if (!ok) {
    console.error(failureMessage(process.versions.node, requiredMajor));
    process.exit(1);
  }
  console.log(`preflight OK — Node ${process.versions.node} satisfies engines.node ${range}`);
}

if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  main();
}
