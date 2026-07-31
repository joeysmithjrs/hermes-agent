import { readFileSync } from 'node:fs';
import { basename, extname } from 'node:path';

import { parse as parseYaml } from 'yaml';

import { SourceSpecError } from '../core/errors.js';
import { parseSourceSpec, type SourceSpec } from '../schema/source-spec.js';

/** Loads and validates a SourceSpec from a YAML or JSON file. */
export function loadSourceSpec(path: string): SourceSpec {
  let text: string;
  try {
    text = readFileSync(path, 'utf8');
  } catch (cause) {
    throw new SourceSpecError(`cannot read source spec at ${path}`, {
      hint: 'Check the path. Sample specs live in specs/sources/.',
      cause,
    });
  }

  let parsed: unknown;
  try {
    parsed = extname(path) === '.json' ? JSON.parse(text) : parseYaml(text);
  } catch (cause) {
    throw new SourceSpecError(`source spec at ${path} is not valid ${extname(path) || 'YAML'}`, {
      hint: 'Fix the syntax error reported by the parser.',
      cause,
    });
  }

  const spec = parseSourceSpec(parsed);
  if (spec.id !== basename(path, extname(path)) && process.env.PM_DESK_STRICT_SPEC_NAMES === '1') {
    throw new SourceSpecError(`spec id ${spec.id} does not match its filename`, {
      hint: 'Rename the file to <id>.yaml, or unset PM_DESK_STRICT_SPEC_NAMES.',
    });
  }
  return spec;
}
