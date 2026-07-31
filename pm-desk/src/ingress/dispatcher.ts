import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { DispatchDisabledError, DispatchError } from '../core/errors.js';
import type { SignalEnvelope } from '../schema/signal.js';
import type { DeskStore } from '../store/index.js';

export interface DispatchResult {
  dispatcher: string;
  artifact_ref?: string;
  detail?: unknown;
}

export interface Dispatcher {
  readonly name: string;
  dispatch(signal: SignalEnvelope): Promise<DispatchResult>;
}

/**
 * The default and only enabled-by-default dispatcher.
 *
 * It writes the envelope to the content-addressed store and queues an outbox
 * row. That is the whole job: it does not call an LLM, start a workflow or
 * reach the network. Handoff to an adjudicator is a separate, explicit step, so
 * a signal arriving at 3am costs nothing and loses nothing.
 */
export class OutboxDispatcher implements Dispatcher {
  readonly name = 'outbox';

  constructor(private readonly store: DeskStore) {}

  async dispatch(signal: SignalEnvelope): Promise<DispatchResult> {
    const artifact = this.store.artifacts.put(JSON.stringify(signal, null, 2), 'application/json');
    this.store.signals.enqueue(signal.signal_id, this.name, artifact.ref, {
      queued_reason: 'awaiting adjudication',
    });
    return { dispatcher: this.name, artifact_ref: artifact.ref };
  }
}

export interface CommandResult {
  code: number;
  stdout: string;
  stderr: string;
}

/** Injectable process runner, so the launcher is testable without spawning. */
export type CommandRunner = (command: string, args: string[]) => Promise<CommandResult>;

export interface HermesLauncherOptions {
  /** Must be explicitly true. Defaults to off. */
  enabled?: boolean;
  binary?: string;
  /** A workflow YAML path (mode 'path') or a registered catalog id ('catalog'). */
  workflow?: string;
  mode?: 'path' | 'catalog';
  maxBudgetUsd?: number;
  runner?: CommandRunner;
  /** Directory for the audit copy of the JSON input handed to Hermes. */
  inputDir?: string;
}

/**
 * OPT-IN launcher that shells out to the Hermes workflow CLI.
 *
 * Disabled unless explicitly enabled (CLI flag or PM_DESK_DISPATCH_HERMES=1).
 * It never reads or writes global Hermes configuration.
 *
 * Argument shape matches the real CLI, which was checked on this host:
 *   hermes workflow run <path.yaml> --input '<json>'
 *   hermes workflow run-catalog <catalog_id> --input '<json>'
 * There is no `--input-file` flag, so the envelope is passed as a single argv
 * element. That is safe because the runner uses execFile, not a shell: the JSON
 * is never parsed by sh and cannot inject anything. A copy is also written to
 * disk and content-addressed in the store, so the exact input that was handed
 * over stays auditable after the run.
 */
export class HermesLauncherDispatcher implements Dispatcher {
  readonly name = 'hermes';
  private readonly enabled: boolean;
  private readonly binary: string;
  private readonly workflow: string;
  private readonly mode: 'path' | 'catalog';
  private readonly maxBudgetUsd?: number;
  private readonly runner: CommandRunner;
  private readonly inputDir?: string;

  constructor(
    private readonly store: DeskStore,
    options: HermesLauncherOptions = {},
  ) {
    this.enabled = options.enabled ?? false;
    this.binary = options.binary ?? 'hermes';
    this.workflow = options.workflow ?? 'workflows/pm-signal-adjudication-v0.yaml';
    this.mode = options.mode ?? 'path';
    this.maxBudgetUsd = options.maxBudgetUsd;
    this.runner = options.runner ?? defaultRunner;
    this.inputDir = options.inputDir;
  }

  async dispatch(signal: SignalEnvelope): Promise<DispatchResult> {
    if (!this.enabled) {
      throw new DispatchDisabledError('the Hermes launcher is disabled', {
        hint: 'Enable it deliberately with `ingress serve --dispatch hermes` or PM_DESK_DISPATCH_HERMES=1. The default outbox dispatcher queues signals without invoking anything.',
      });
    }

    // The workflow is a single agent node with `tools: []`, so it cannot fetch
    // its own evidence. We render the prompt here — deterministic code we own
    // and test — and hand the finished text over as workflow input.
    const { renderAdjudicationPrompt } = await import('../workflow/prompt.js');
    const input = {
      signal_id: signal.signal_id,
      prompt: renderAdjudicationPrompt(this.store, signal),
      paper_only: true as const,
    };
    const payload = JSON.stringify(input);

    const dir = this.inputDir ?? mkdtempSync(join(tmpdir(), 'pm-desk-signal-'));
    const inputPath = join(dir, `${signal.signal_id}.json`);
    writeFileSync(inputPath, JSON.stringify(input, null, 2), 'utf8');

    const artifact = this.store.artifacts.put(JSON.stringify(signal, null, 2), 'application/json');
    const outboxId = this.store.signals.enqueue(signal.signal_id, this.name, artifact.ref, {
      workflow: this.workflow,
      mode: this.mode,
      input_path: inputPath,
    });

    const args = [
      'workflow',
      this.mode === 'catalog' ? 'run-catalog' : 'run',
      this.workflow,
      '--input',
      payload,
      ...(this.maxBudgetUsd !== undefined ? ['--max-budget-usd', String(this.maxBudgetUsd)] : []),
    ];
    const result = await this.runner(this.binary, args);

    if (result.code !== 0) {
      this.store.signals.markOutbox(outboxId, 'failed', {
        code: result.code,
        stderr: result.stderr.slice(0, 2000),
      });
      throw new DispatchError(
        `hermes workflow run exited ${result.code}: ${result.stderr.trim() || '(no stderr)'}`,
        {
          hint: 'The signal is recorded and the outbox row is marked failed — nothing was lost. Check `hermes workflow list` and retry with `pm-desk ingress outbox`.',
        },
      );
    }

    this.store.signals.markOutbox(outboxId, 'dispatched', { stdout: result.stdout.slice(0, 2000) });
    return {
      dispatcher: this.name,
      artifact_ref: artifact.ref,
      detail: { workflow: this.workflow },
    };
  }
}

const defaultRunner: CommandRunner = async (command, args) => {
  const { execFile } = await import('node:child_process');
  return new Promise((resolve) => {
    // execFile, not exec: arguments are passed as an argv array, never a shell string.
    execFile(command, args, { timeout: 120_000 }, (error, stdout, stderr) => {
      resolve({
        code: error && typeof error.code === 'number' ? error.code : error ? 1 : 0,
        stdout: String(stdout),
        stderr: String(stderr),
      });
    });
  });
};

/** Builds the dispatcher named by config, defaulting to the safe one. */
export function resolveDispatcher(
  store: DeskStore,
  name: string | undefined,
  env: Record<string, string | undefined> = process.env,
): Dispatcher {
  const requested = name ?? (env.PM_DESK_DISPATCH_HERMES === '1' ? 'hermes' : 'outbox');
  if (requested === 'outbox') return new OutboxDispatcher(store);
  if (requested === 'hermes') {
    return new HermesLauncherDispatcher(store, {
      enabled: env.PM_DESK_DISPATCH_HERMES === '1' || name === 'hermes',
      binary: env.PM_DESK_HERMES_BIN ?? 'hermes',
      workflow: env.PM_DESK_HERMES_WORKFLOW ?? 'workflows/pm-signal-adjudication-v0.yaml',
      mode: env.PM_DESK_HERMES_MODE === 'catalog' ? 'catalog' : 'path',
    });
  }
  throw new DispatchError(`unknown dispatcher: ${requested}`, {
    hint: 'Supported dispatchers: outbox (default), hermes (opt-in).',
  });
}
