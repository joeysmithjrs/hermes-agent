import { UsageError } from '../../core/errors.js';
import { createDeskPublicClient } from '../../polymarket/client.js';
import { collectMarkets, snapshotTokens } from '../../polymarket/collector.js';
import { describeRealtimeCapability } from '../../polymarket/realtime.js';
import { openStore } from '../../store/index.js';
import type { Flags } from '../args.js';
import { emit, table } from '../output.js';

export async function marketCommand(sub: string | undefined, flags: Flags): Promise<number> {
  const json = flags.bool('json');
  const home = flags.str('home');

  switch (sub) {
    case 'discover': {
      const limit = flags.int('limit', 10)!;
      const pageSize = flags.int('page-size', Math.min(limit, 25))!;
      const dryRun = flags.bool('dry-run');
      const includeClosed = flags.bool('include-closed');
      flags.rejectUnknown('market discover');

      if (limit <= 0 || limit > 500) {
        throw new UsageError('--limit must be between 1 and 500', {
          hint: 'Discovery is deliberately bounded. Use several smaller runs if you need more.',
        });
      }

      const store = openStore({ home });
      const client = createDeskPublicClient();
      try {
        const result = await collectMarkets(store, client, {
          limit,
          pageSize,
          dryRun,
          closed: includeClosed ? undefined : false,
        });
        emit({ json }, { ...result, paper_only: true }, () =>
          [
            `${dryRun ? '[dry-run] would store' : 'stored'} ${result.markets} markets / ${result.tokens} outcome tokens (${result.pages} page(s))`,
            result.skipped.length > 0 ? `skipped: ${result.skipped.length}` : '',
            '',
            table(
              result.sample.map((s) => ({
                market_id: s.market_id,
                question: s.question.slice(0, 70),
                tokens: s.token_ids.length,
              })),
              ['market_id', 'question', 'tokens'],
            ),
          ]
            .filter(Boolean)
            .join('\n'),
        );
      } finally {
        store.close();
      }
      return 0;
    }

    case 'snapshot': {
      const tokens = flags.list('token');
      const marketId = flags.str('market');
      const dryRun = flags.bool('dry-run');
      flags.rejectUnknown('market snapshot');

      const store = openStore({ home });
      try {
        let tokenIds = tokens;
        if (tokenIds.length === 0 && marketId) {
          tokenIds = store.markets.listTokens(marketId).map((t) => t.token_id);
        }
        if (tokenIds.length === 0) {
          throw new UsageError('no tokens to snapshot', {
            hint: 'Pass --token <id> (repeatable) or --market <market_id> after running `market discover`.',
          });
        }
        const client = createDeskPublicClient();
        const result = await snapshotTokens(store, client, tokenIds, { dryRun });
        emit({ json }, { ...result, paper_only: true }, () =>
          [
            `${dryRun ? '[dry-run] would append' : 'appended'} ${result.observed} observation(s)`,
            result.failed.length > 0
              ? `failed: ${result.failed.map((f) => `${f.token_id} (${f.error})`).join('; ')}`
              : '',
            result.gaps.length > 0
              ? `capability gaps: ${result.gaps.map((g) => `${g.capability}@${g.token_id}`).join(', ')}`
              : '',
            '',
            table(result.snapshots, ['token_id', 'observed_at', 'mid', 'spread']),
          ]
            .filter(Boolean)
            .join('\n'),
        );
      } finally {
        store.close();
      }
      return 0;
    }

    case 'capability': {
      flags.rejectUnknown('market capability');
      const client = createDeskPublicClient();
      const capability = describeRealtimeCapability(client);
      emit({ json }, { client: client.kind, ...capability, paper_only: true }, () =>
        [
          `client            ${client.kind} (public, read-only)`,
          `subscriptions     ${capability.subscriptions ? 'available' : 'not available'}`,
          `active data path  ${capability.active}`,
          `note              ${capability.note}`,
        ].join('\n'),
      );
      return 0;
    }

    case 'list': {
      const limit = flags.int('limit', 20)!;
      flags.rejectUnknown('market list');
      const store = openStore({ home, readonly: true });
      const markets = store.markets.listMarkets({ limit, openOnly: true });
      const rows = markets.map((m) => ({
        market_id: m.market_id,
        question: m.question.slice(0, 60),
        end_date: m.end_date ?? '',
        tokens: store.markets.listTokens(m.market_id).length,
      }));
      store.close();
      emit({ json }, { count: rows.length, markets: rows }, () =>
        table(rows, ['market_id', 'question', 'end_date', 'tokens']),
      );
      return 0;
    }

    default:
      throw new UsageError(`unknown \`market\` subcommand: ${sub ?? '(none)'}`, {
        hint: 'Try: pm-desk market discover --limit 5 | market snapshot --token <id> | market list | market capability',
      });
  }
}
