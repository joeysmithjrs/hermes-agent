import { UsageError } from '../../core/errors.js';
import { evaluateMonitor } from '../../monitor/engine.js';
import { loadMonitorSpec, loadMonitorSpecDir } from '../../monitor/spec-loader.js';
import { openStore } from '../../store/index.js';
import type { Flags } from '../args.js';
import { emit, table } from '../output.js';

export async function monitorCommand(sub: string | undefined, flags: Flags): Promise<number> {
  const json = flags.bool('json');
  const home = flags.str('home');

  switch (sub) {
    case 'evaluate': {
      const specPath = flags.str('spec');
      const dir = flags.str('dir');
      const dryRun = flags.bool('dry-run');
      flags.rejectUnknown('monitor evaluate');

      if (!specPath && !dir) {
        throw new UsageError('monitor evaluate needs --spec <file> or --dir <directory>', {
          hint: 'Sample specs live in specs/monitors/.',
        });
      }

      const specs = specPath
        ? [{ path: specPath, spec: loadMonitorSpec(specPath) }]
        : loadMonitorSpecDir(dir!);

      const store = openStore({ home });
      try {
        const results = specs.map(({ spec }) => evaluateMonitor(store, spec, { dryRun }));
        const emitted = results.filter((r) => r.outcome === 'emitted');

        if (json) {
          // Machine mode: only signals, so a consumer can pipe straight to ingress.
          process.stdout.write(
            `${JSON.stringify(emitted.length === 1 ? emitted[0]!.signal : emitted.map((r) => r.signal), null, 2)}\n`,
          );
          return emitted.length > 0 ? 0 : 0;
        }

        if (emitted.length === 0) {
          // The single most common outcome, and it must be unmistakable.
          process.stdout.write('SILENT\n');
          for (const r of results) {
            process.stdout.write(
              `  ${r.rule_id}: ${r.outcome}${r.reason ? ` — ${r.reason}` : ''}\n`,
            );
          }
          return 0;
        }

        for (const result of emitted) {
          process.stdout.write(`${JSON.stringify(result.signal, null, 2)}\n`);
        }
        const others = results.filter((r) => r.outcome !== 'emitted');
        if (others.length > 0) {
          process.stdout.write(
            `\n${others.map((r) => `  ${r.rule_id}: ${r.outcome}`).join('\n')}\n`,
          );
        }
        return 0;
      } finally {
        store.close();
      }
    }

    case 'list': {
      const dir = flags.str('dir', 'specs/monitors')!;
      flags.rejectUnknown('monitor list');
      const specs = loadMonitorSpecDir(dir);
      const rows = specs.map(({ path, spec }) => ({
        id: spec.id,
        kind: spec.kind,
        severity: spec.severity,
        enabled: spec.enabled ? 'yes' : 'no',
        cooldown_s: spec.cooldown_s,
        path,
      }));
      emit({ json }, { count: rows.length, monitors: rows }, () =>
        table(rows, ['id', 'kind', 'severity', 'enabled', 'cooldown_s']),
      );
      return 0;
    }

    case 'decisions': {
      const ruleId = flags.str('rule');
      const limit = flags.int('limit', 50)!;
      flags.rejectUnknown('monitor decisions');
      const store = openStore({ home, readonly: true });
      const rows = store.monitorState.listDecisions(ruleId, limit).map((d) => ({
        evaluated_at: d.evaluated_at,
        rule_id: d.rule_id,
        outcome: d.outcome,
        reason: (d.reason ?? '').slice(0, 60),
      }));
      store.close();
      emit({ json }, { count: rows.length, decisions: rows }, () =>
        table(rows, ['evaluated_at', 'rule_id', 'outcome', 'reason']),
      );
      return 0;
    }

    default:
      throw new UsageError(`unknown \`monitor\` subcommand: ${sub ?? '(none)'}`, {
        hint: 'Try: pm-desk monitor evaluate --spec <file> | monitor list | monitor decisions',
      });
  }
}
