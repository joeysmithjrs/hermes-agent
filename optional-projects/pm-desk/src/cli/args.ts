import { UsageError } from '../core/errors.js';

/**
 * Minimal, dependency-free flag parsing. Supports `--flag`, `--key value`,
 * `--key=value` and repeated keys (collected into an array). Unknown flags are
 * an error rather than a silent no-op, so a typo in a `--limit` never quietly
 * runs an unbounded collection.
 */
export interface ParsedArgs {
  positionals: string[];
  flags: Map<string, string[]>;
}

export function parseArgs(argv: readonly string[]): ParsedArgs {
  const positionals: string[] = [];
  const flags = new Map<string, string[]>();

  for (let i = 0; i < argv.length; i += 1) {
    const token = argv[i]!;
    if (!token.startsWith('--')) {
      positionals.push(token);
      continue;
    }
    const body = token.slice(2);
    const eq = body.indexOf('=');
    if (eq >= 0) {
      push(flags, body.slice(0, eq), body.slice(eq + 1));
      continue;
    }
    const next = argv[i + 1];
    if (next === undefined || next.startsWith('--')) {
      push(flags, body, 'true');
    } else {
      push(flags, body, next);
      i += 1;
    }
  }

  return { positionals, flags };
}

function push(flags: Map<string, string[]>, key: string, value: string): void {
  const existing = flags.get(key);
  if (existing) existing.push(value);
  else flags.set(key, [value]);
}

export class Flags {
  private readonly used = new Set<string>();

  constructor(private readonly parsed: ParsedArgs) {}

  get positionals(): string[] {
    return this.parsed.positionals;
  }

  str(name: string, fallback?: string): string | undefined {
    this.used.add(name);
    const values = this.parsed.flags.get(name);
    if (!values || values.length === 0) return fallback;
    return values[values.length - 1];
  }

  required(name: string, hint: string): string {
    const value = this.str(name);
    if (value === undefined) {
      throw new UsageError(`missing required flag --${name}`, { hint });
    }
    return value;
  }

  list(name: string): string[] {
    this.used.add(name);
    const values = this.parsed.flags.get(name) ?? [];
    return values
      .flatMap((v) => v.split(','))
      .map((v) => v.trim())
      .filter(Boolean);
  }

  bool(name: string, fallback = false): boolean {
    this.used.add(name);
    const values = this.parsed.flags.get(name);
    if (!values || values.length === 0) return fallback;
    const last = values[values.length - 1]!.toLowerCase();
    return last === 'true' || last === '1' || last === 'yes';
  }

  int(name: string, fallback?: number): number | undefined {
    const raw = this.str(name);
    if (raw === undefined) return fallback;
    const value = Number(raw);
    if (!Number.isInteger(value)) {
      throw new UsageError(`--${name} must be an integer, got ${JSON.stringify(raw)}`, {
        hint: `Example: --${name} 10`,
      });
    }
    return value;
  }

  /** Call after reading every flag a command supports. */
  rejectUnknown(command: string): void {
    const unknown = [...this.parsed.flags.keys()].filter((key) => !this.used.has(key));
    if (unknown.length > 0) {
      throw new UsageError(
        `unknown flag${unknown.length > 1 ? 's' : ''} for \`${command}\`: ${unknown
          .map((u) => `--${u}`)
          .join(', ')}`,
        { hint: `Run \`pm-desk ${command} --help\` to see supported flags.` },
      );
    }
  }
}
