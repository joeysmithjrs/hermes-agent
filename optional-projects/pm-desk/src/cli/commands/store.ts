import { UsageError } from '../../core/errors.js';
import { openStore } from '../../store/index.js';
import type { Flags } from '../args.js';
import { emit, table } from '../output.js';

const EXPORTABLE = [
  'markets',
  'outcome_tokens',
  'market_observations',
  'source_snapshots',
  'signals',
  'signal_outbox',
  'monitor_decisions',
  'monitor_state',
  'adjudications',
  'paper_ledger',
  'paper_ledger_annotations',
  'artifacts',
] as const;

export async function storeCommand(sub: string | undefined, flags: Flags): Promise<number> {
  const json = flags.bool('json');
  const home = flags.str('home');

  switch (sub) {
    case 'init': {
      flags.rejectUnknown('store init');
      const store = openStore({ home });
      const payload = {
        home: store.home,
        db: store.dbPath,
        artifacts: store.artifactsRoot,
        schema_version: store.schemaVersion(),
      };
      store.close();
      emit({ json }, payload, () =>
        [
          `desk home        ${payload.home}`,
          `database         ${payload.db}`,
          `artifacts        ${payload.artifacts}`,
          `schema version   ${payload.schema_version}`,
        ].join('\n'),
      );
      return 0;
    }

    case 'status': {
      flags.rejectUnknown('store status');
      const store = openStore({ home });
      const counts: Record<string, number> = {};
      for (const t of EXPORTABLE) {
        counts[t] = (store.db.prepare(`SELECT COUNT(*) AS n FROM ${t}`).get() as { n: number }).n;
      }
      const payload = {
        home: store.home,
        schema_version: store.schemaVersion(),
        paper_only: true,
        counts,
      };
      store.close();
      emit({ json }, payload, () =>
        [
          `desk home       ${payload.home}  (schema v${payload.schema_version}, PAPER ONLY)`,
          '',
          table(
            Object.entries(counts).map(([k, v]) => ({ table: k, rows: v })),
            ['table', 'rows'],
          ),
        ].join('\n'),
      );
      return 0;
    }

    case 'export': {
      const tableName = flags.required('table', `One of: ${EXPORTABLE.join(', ')}`);
      const limit = flags.int('limit', 100)!;
      flags.rejectUnknown('store export');
      if (!(EXPORTABLE as readonly string[]).includes(tableName)) {
        throw new UsageError(`--table ${tableName} is not exportable`, {
          hint: `Exportable tables: ${EXPORTABLE.join(', ')}`,
        });
      }
      const store = openStore({ home, readonly: true });
      const rows = store.db.prepare(`SELECT * FROM ${tableName} LIMIT ?`).all(limit);
      store.close();
      // Export is always JSON-shaped; --json only removes the human header.
      emit({ json: true }, { table: tableName, count: rows.length, rows }, () => '');
      return 0;
    }

    default:
      throw new UsageError(`unknown \`store\` subcommand: ${sub ?? '(none)'}`, {
        hint: 'Try: pm-desk store init | store status | store export --table markets',
      });
  }
}
