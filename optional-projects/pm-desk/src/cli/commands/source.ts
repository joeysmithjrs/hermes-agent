import { UsageError } from '../../core/errors.js';
import { LIVE_CONFIRM_FLAG, resolveBrowser } from '../../browserbase/browser-factory.js';
import { collectSource } from '../../browserbase/collector.js';
import { assertNavigable } from '../../browserbase/policy.js';
import { loadSourceSpec } from '../../browserbase/spec-loader.js';
import { openStore } from '../../store/index.js';
import type { Flags } from '../args.js';
import { emit, table } from '../output.js';

export async function sourceCommand(sub: string | undefined, flags: Flags): Promise<number> {
  const json = flags.bool('json');
  const home = flags.str('home');

  switch (sub) {
    case 'validate': {
      const specPath = flags.required('spec', 'Path to a SourceSpec YAML file.');
      flags.rejectUnknown('source validate');
      const spec = loadSourceSpec(specPath);
      // Policy is part of validity: a spec pointing at a sensitive route is invalid.
      assertNavigable(spec.url, spec.allowed_domains);
      emit({ json }, { valid: true, spec, paper_only: true }, () =>
        [
          `spec ${spec.id} v${spec.version} is valid`,
          `url             ${spec.url}`,
          `allowed domains ${spec.allowed_domains.join(', ')}`,
          `text selector   ${spec.extract.text_selector}`,
          `fields          ${Object.keys(spec.extract.fields).join(', ') || '(none)'}`,
          `fingerprint     ${spec.fingerprint}`,
        ].join('\n'),
      );
      return 0;
    }

    case 'collect': {
      const specPath = flags.required('spec', 'Path to a SourceSpec YAML file.');
      const fixture = flags.str('fixture');
      const live = flags.bool('live');
      const confirmed = flags.bool(LIVE_CONFIRM_FLAG);
      const dryRun = flags.bool('dry-run');
      flags.rejectUnknown('source collect');

      const spec = loadSourceSpec(specPath);
      const browser = await resolveBrowser({ live, confirmed, spec, fixture });
      const store = openStore({ home });
      try {
        const result = await collectSource(store, spec, { browser, dryRun });
        emit({ json }, { ...result, paper_only: true }, () =>
          [
            `${result.dryRun ? '[dry-run] ' : ''}${result.source_id} v${result.spec_version} — ${
              result.changed ? 'CHANGED' : 'unchanged'
            } (${result.mode})`,
            `collected_at   ${result.collected_at}`,
            `content_hash   ${result.content_hash}`,
            `previous_hash  ${result.previous_hash ?? '(none — first collection)'}`,
            `artifact       ${result.normalized_artifact_ref ?? '(not stored — dry run)'}`,
            '',
            table(
              Object.entries(result.fields).map(([field, value]) => ({
                field,
                value: value ?? '(null)',
              })),
              ['field', 'value'],
            ),
          ].join('\n'),
        );
      } finally {
        store.close();
      }
      return 0;
    }

    case 'history': {
      const sourceId = flags.required('id', 'A source id, e.g. example_official_release.');
      const limit = flags.int('limit', 20)!;
      flags.rejectUnknown('source history');
      const store = openStore({ home, readonly: true });
      const rows = store.sources.history(sourceId, limit).map((r) => ({
        collected_at: r.collected_at,
        changed: r.changed ? 'yes' : 'no',
        content_hash: r.content_hash.slice(0, 16),
        mode: r.mode,
      }));
      store.close();
      emit({ json }, { source_id: sourceId, count: rows.length, snapshots: rows }, () =>
        table(rows, ['collected_at', 'changed', 'content_hash', 'mode']),
      );
      return 0;
    }

    case 'list': {
      flags.rejectUnknown('source list');
      const store = openStore({ home, readonly: true });
      const rows = store.sources.listSources().map((id) => {
        const latest = store.sources.latest(id)!;
        return {
          source_id: id,
          latest_at: latest.collected_at,
          content_hash: latest.content_hash.slice(0, 16),
        };
      });
      store.close();
      emit({ json }, { count: rows.length, sources: rows }, () =>
        table(rows, ['source_id', 'latest_at', 'content_hash']),
      );
      return 0;
    }

    default:
      throw new UsageError(`unknown \`source\` subcommand: ${sub ?? '(none)'}`, {
        hint: 'Try: pm-desk source validate --spec <file> | source collect --spec <file> --fixture <html> | source history --id <id> | source list',
      });
  }
}
