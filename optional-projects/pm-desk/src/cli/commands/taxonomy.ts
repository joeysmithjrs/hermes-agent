import { join } from 'node:path';

import { UsageError } from '../../core/errors.js';
import { openStore } from '../../store/index.js';
import {
  compileSeed,
  deskStateHash,
  detectCapabilities,
  loadArenas,
  loadTaxonomy,
  SEED_MODES,
  type DeskCapability,
  type SeedMode,
} from '../../taxonomy/index.js';
import type { Flags } from '../args.js';
import { emit, table } from '../output.js';

const DEFAULT_TAXONOMY = join(process.cwd(), 'taxonomy', 'cards.yaml');
const DEFAULT_ARENAS = join(process.cwd(), 'taxonomy', 'arenas.yaml');

/**
 * The morning wants one mission across several arenas, so that is what this
 * command produces unless an operator asks for the older breadth-first survey
 * with `--mode stratify_families`.
 */
const CLI_DEFAULT_MODE: SeedMode = 'mission_x_arena';

export async function taxonomyCommand(sub: string | undefined, flags: Flags): Promise<number> {
  const json = flags.bool('json');
  const home = flags.str('home');
  const taxonomyPath = flags.str('taxonomy', DEFAULT_TAXONOMY)!;
  const arenasPath = flags.str('arenas', DEFAULT_ARENAS)!;

  switch (sub) {
    case 'compile': {
      const runDate = flags.str('date', new Date().toISOString().slice(0, 10))!;
      const nonce = flags.str('nonce');
      const maxCards = flags.int('max-cards', 4)!;
      const capabilityOverride = flags.list('capability');
      const mode = parseMode(flags.str('mode', CLI_DEFAULT_MODE)!);
      const mission = flags.str('mission');
      const includeExplore = flags.bool('include-explore');
      flags.rejectUnknown('taxonomy compile');

      const store = openStore({ home, readonly: true });
      const stateHash = deskStateHash(store);
      store.close();

      const capabilities = (
        capabilityOverride.length > 0 ? capabilityOverride : detectCapabilities()
      ) as DeskCapability[];

      const result = compileSeed({
        taxonomyPath,
        arenasPath,
        runDate,
        deskStateHash: stateHash,
        capabilities,
        maxCards,
        mode,
        ...(nonce ? { nonce } : {}),
        ...(mission ? { mission } : {}),
        ...(includeExplore ? { includeExplore } : {}),
      });

      emit({ json }, result, () =>
        [
          `directive seed ${result.seed.slice(0, 16)}  (mode ${result.mode}, taxonomy v${result.taxonomy_version}${
            result.arenas_version !== undefined ? `, arenas v${result.arenas_version}` : ''
          }, PAPER ONLY)`,
          `run date ${result.run_date} · desk state ${result.desk_state_hash.slice(0, 12)}${nonce ? ` · nonce ${nonce}` : ''}`,
          `selection ${result.selection_hash.slice(0, 16)}`,
          `capabilities: ${result.capabilities.join(', ')}`,
          result.mission
            ? `mission: ${result.mission.family_id}/${result.mission.card_id} across arenas [${(result.arena_ids ?? []).join(', ')}]`
            : '',
          '',
          table(
            result.cards.map((c) => ({
              family: c.family_id,
              arena: c.arena_id ?? '-',
              card: c.card_id,
              title: c.title,
            })),
            ['family', 'arena', 'card', 'title'],
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
          mission: family.mission_eligible ? 'yes' : '',
          card: card.id,
          requires: card.requires.join(','),
        })),
      );
      emit({ json }, { version: taxonomy.version, count: rows.length, cards: rows }, () =>
        table(rows, ['family', 'status', 'mission', 'card', 'requires']),
      );
      return 0;
    }

    case 'arenas': {
      flags.rejectUnknown('taxonomy arenas');
      const set = loadArenas(arenasPath);
      const rows = set.arenas.map((arena) => ({
        arena: arena.id,
        title: arena.title,
        tags: arena.gamma_tag_any.join(','),
        slug_includes: arena.slug_includes.join(','),
        min_liquidity_usd: arena.min_liquidity_usd ?? '',
      }));
      emit({ json }, { version: set.version, count: rows.length, arenas: set.arenas }, () =>
        table(rows, ['arena', 'title', 'tags', 'min_liquidity_usd']),
      );
      return 0;
    }

    default:
      throw new UsageError(`unknown \`taxonomy\` subcommand: ${sub ?? '(none)'}`, {
        hint: 'Try: pm-desk taxonomy compile --mode mission_x_arena --max-cards 3 | taxonomy list | taxonomy arenas',
      });
  }
}

function parseMode(raw: string): SeedMode {
  if ((SEED_MODES as readonly string[]).includes(raw)) return raw as SeedMode;
  throw new UsageError(`unknown seed mode: ${raw}`, {
    hint: `Known modes: ${SEED_MODES.join(', ')}.`,
  });
}
