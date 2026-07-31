import { join } from 'node:path';

import { UsageError } from '../../core/errors.js';
import { openStore } from '../../store/index.js';
import {
  compileSeed,
  deskStateHash,
  detectCapabilities,
  loadTaxonomy,
  type DeskCapability,
} from '../../taxonomy/index.js';
import type { Flags } from '../args.js';
import { emit, table } from '../output.js';

const DEFAULT_TAXONOMY = join(process.cwd(), 'taxonomy', 'cards.yaml');

export async function taxonomyCommand(sub: string | undefined, flags: Flags): Promise<number> {
  const json = flags.bool('json');
  const home = flags.str('home');
  const taxonomyPath = flags.str('taxonomy', DEFAULT_TAXONOMY)!;

  switch (sub) {
    case 'compile': {
      const runDate = flags.str('date', new Date().toISOString().slice(0, 10))!;
      const nonce = flags.str('nonce');
      const maxCards = flags.int('max-cards', 4)!;
      const capabilityOverride = flags.list('capability');
      flags.rejectUnknown('taxonomy compile');

      const store = openStore({ home, readonly: true });
      const stateHash = deskStateHash(store);
      store.close();

      const capabilities = (
        capabilityOverride.length > 0 ? capabilityOverride : detectCapabilities()
      ) as DeskCapability[];

      const result = compileSeed({
        taxonomyPath,
        runDate,
        deskStateHash: stateHash,
        capabilities,
        maxCards,
        ...(nonce ? { nonce } : {}),
      });

      emit({ json }, result, () =>
        [
          `directive seed ${result.seed.slice(0, 16)}  (taxonomy v${result.taxonomy_version}, PAPER ONLY)`,
          `run date ${result.run_date} · desk state ${result.desk_state_hash.slice(0, 12)}${nonce ? ` · nonce ${nonce}` : ''}`,
          `capabilities: ${result.capabilities.join(', ')}`,
          '',
          table(
            result.cards.map((c) => ({
              family: c.family_id,
              card: c.card_id,
              title: c.title,
            })),
            ['family', 'card', 'title'],
          ),
          '',
          ...result.cards.map(
            (c) => `▸ ${c.card_id}\n  ${c.directive}\n  prohibits: ${c.prohibits.join(', ')}`,
          ),
          '',
          result.deferred.length > 0
            ? `deferred families: ${result.deferred.map((d) => `${d.family_id} (${d.activate_with ?? 'no capability named'})`).join('; ')}`
            : '',
          `excluded cards: ${result.excluded.length}`,
        ]
          .filter(Boolean)
          .join('\n'),
      );
      return 0;
    }

    case 'list': {
      flags.rejectUnknown('taxonomy list');
      const taxonomy = loadTaxonomy(taxonomyPath);
      const rows = taxonomy.families.flatMap((family) =>
        family.cards.map((card) => ({
          family: family.id,
          status: family.status,
          card: card.id,
          requires: card.requires.join(','),
        })),
      );
      emit({ json }, { version: taxonomy.version, count: rows.length, cards: rows }, () =>
        table(rows, ['family', 'status', 'card', 'requires']),
      );
      return 0;
    }

    default:
      throw new UsageError(`unknown \`taxonomy\` subcommand: ${sub ?? '(none)'}`, {
        hint: 'Try: pm-desk taxonomy compile --max-cards 4 | taxonomy list',
      });
  }
}
