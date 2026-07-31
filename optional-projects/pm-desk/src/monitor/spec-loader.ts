import { readdirSync, readFileSync, statSync } from 'node:fs';
import { extname, join } from 'node:path';

import { parse as parseYaml } from 'yaml';

import { MonitorSpecError } from '../core/errors.js';
import { parseMonitorSpec, type MonitorSpec } from '../schema/monitor-spec.js';

export function loadMonitorSpec(path: string): MonitorSpec {
  let text: string;
  try {
    text = readFileSync(path, 'utf8');
  } catch (cause) {
    throw new MonitorSpecError(`cannot read monitor spec at ${path}`, {
      hint: 'Check the path. Sample specs live in specs/monitors/.',
      cause,
    });
  }

  let parsed: unknown;
  try {
    parsed = extname(path) === '.json' ? JSON.parse(text) : parseYaml(text);
  } catch (cause) {
    throw new MonitorSpecError(`monitor spec at ${path} is not valid ${extname(path) || 'YAML'}`, {
      hint: 'Fix the syntax error reported by the parser.',
      cause,
    });
  }

  return parseMonitorSpec(parsed);
}

export function loadMonitorSpecDir(dir: string): { path: string; spec: MonitorSpec }[] {
  let entries: string[];
  try {
    entries = readdirSync(dir);
  } catch (cause) {
    throw new MonitorSpecError(`cannot read monitor spec directory ${dir}`, {
      hint: 'Create it, or point --dir at specs/monitors/.',
      cause,
    });
  }

  return entries
    .filter((name) => ['.yaml', '.yml', '.json'].includes(extname(name)))
    .map((name) => join(dir, name))
    .filter((path) => statSync(path).isFile())
    .sort()
    .map((path) => ({ path, spec: loadMonitorSpec(path) }));
}
