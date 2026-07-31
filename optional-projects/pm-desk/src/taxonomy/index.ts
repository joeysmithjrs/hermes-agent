import { readFileSync } from 'node:fs';

import { parse as parseYaml } from 'yaml';
import { z } from 'zod';

import { TaxonomyError } from '../core/errors.js';
import { objectHash, sha256Hex } from '../core/hash.js';
import { validate } from '../schema/common.js';
import type { DeskStore } from '../store/index.js';
import { arenaScopeBlock, loadArenas, type Arena, type ArenaSet } from './arenas.js';

export { loadArenas, findArena, arenaScopeBlock } from './arenas.js';
export type { Arena, ArenaSet } from './arenas.js';

/**
 * Directive taxonomy and its deterministic seed compiler.
 *
 * The point of determinism here is auditability: given a run date, a desk-state
 * hash and an optional nonce, the same cards come out every time, so a recorded
 * research plan can be reproduced and argued with later.
 *
 * Two seed modes:
 *
 *   stratify_families  one card per family, breadth across methods. This is
 *                      what v2 did, and it is still the right shape when the
 *                      desk wants to survey what it could be doing.
 *
 *   mission_x_arena    ONE mission (say fair-value research) run against N
 *                      different Polymarket arenas in parallel. The branches
 *                      then share a method and differ only in where they look,
 *                      which is what makes their candidates comparable at the
 *                      gate. This is the intended morning shape.
 *
 * `compileSeed` still defaults to stratify_families so existing callers keep
 * their behaviour; `pm-desk taxonomy compile` defaults to mission_x_arena.
 */

export const ALL_CAPABILITIES = [
  'polymarket_public_data',
  'primary_source_collection',
  'local_evidence_store',
  'paper_ledger',
  'cross_venue_entailment',
  'neg_risk_market_making',
] as const;

export type DeskCapability = (typeof ALL_CAPABILITIES)[number];

/** Things a card may forbid. `live_execution` is implicit on every card. */
const PROHIBITIONS = ['live_execution', 'uma_propose_dispute', 'blind_copy_execution'] as const;

const CardSchema = z
  .object({
    id: z.string().min(1),
    title: z.string().min(1),
    requires: z.array(z.string().min(1)).default([]),
    prohibits: z.array(z.enum(PROHIBITIONS)).default([]),
    /** Relative pick weight inside a family (default 1). Higher = more often seeded. */
    weight: z.number().positive().default(1),
    directive: z.string().min(10),
    deliverable: z.string().min(5).optional(),
  })
  .strict();

const FamilySchema = z
  .object({
    id: z.string().min(1),
    title: z.string().min(1).optional(),
    status: z.enum(['active', 'deferred']),
    paper_only: z.boolean().default(true),
    description: z.string().optional(),
    deferred_reason: z.string().optional(),
    requires_capability_to_activate: z.string().optional(),
    human_only: z.array(z.string()).default([]),
    always_reserve_slot: z.boolean().default(false),
    /**
     * May this family be the single mission a mission_x_arena morning runs
     * across every arena? True only for methods that mean the same thing in
     * pop culture as they do in macro — an exploration seed or a buildout
     * proposal does not.
     */
    mission_eligible: z.boolean().default(false),
    cards: z.array(CardSchema).min(1),
  })
  .strict();

const TaxonomySchema = z
  .object({
    version: z.number().int().positive(),
    families: z.array(FamilySchema).min(1),
  })
  .strict();

export type TaxonomyCard = z.infer<typeof CardSchema>;
export type TaxonomyFamily = z.infer<typeof FamilySchema>;
export type Taxonomy = z.infer<typeof TaxonomySchema>;

export interface LoadOptions {
  /** Used by tests to validate a document without writing it to disk. */
  overrideDocument?: unknown;
}

export function loadTaxonomy(path: string, options: LoadOptions = {}): Taxonomy {
  let document = options.overrideDocument;
  if (document === undefined) {
    try {
      document = parseYaml(readFileSync(path, 'utf8'));
    } catch (cause) {
      throw new TaxonomyError(`cannot read the taxonomy at ${path}`, {
        hint: 'The shipped taxonomy lives at taxonomy/cards.yaml.',
        cause,
      });
    }
  }

  const taxonomy = validate(TaxonomySchema, document, 'Taxonomy');

  // A card requiring a capability the desk cannot even name is a typo, and a
  // typo here silently removes a card from every future seed.
  for (const family of taxonomy.families) {
    for (const card of family.cards) {
      for (const requirement of card.requires) {
        if (!(ALL_CAPABILITIES as readonly string[]).includes(requirement)) {
          throw new TaxonomyError(
            `card ${family.id}/${card.id} requires unknown capability "${requirement}"`,
            { hint: `Known capabilities: ${ALL_CAPABILITIES.join(', ')}` },
          );
        }
      }
    }
  }

  return taxonomy;
}

export const SEED_MODES = ['stratify_families', 'mission_x_arena'] as const;
export type SeedMode = (typeof SEED_MODES)[number];

export const DEFAULT_SEED_MODE: SeedMode = 'stratify_families';

export interface CompileSeedInput {
  taxonomyPath: string;
  /** ISO date (YYYY-MM-DD) the run belongs to. */
  runDate: string;
  /** Fingerprint of desk state, so the plan moves as the desk learns. */
  deskStateHash: string;
  capabilities: DeskCapability[];
  maxCards: number;
  /** Optional operator nonce, to deliberately re-roll a plan. */
  nonce?: string;
  taxonomy?: Taxonomy;
  /** Defaults to `stratify_families`; the CLI passes `mission_x_arena`. */
  mode?: SeedMode;
  /** Only read in `mission_x_arena` mode. */
  arenasPath?: string;
  arenas?: ArenaSet;
  /** Force the mission: a family id, or a card id inside a mission family. */
  mission?: string;
  /** Spend one of the card slots on an exploration seed instead of an arena. */
  includeExplore?: boolean;
}

export interface SeededCard {
  family_id: string;
  card_id: string;
  title: string;
  directive: string;
  deliverable?: string;
  prohibits: string[];
  human_only: string[];
  paper_only: true;
  /**
   * Always present, in both modes, because a prompt that references a field
   * some cards lack fails that branch outright rather than degrading.
   */
  arena_label: string;
  /**
   * mission_x_arena only. `card_id` is `<mission_card_id>__<arena_id>` so a
   * branch, its workspace artifact and its fanout key stay distinguishable,
   * while these fields let a prompt or tool address either half directly.
   */
  mission_family_id?: string;
  mission_card_id?: string;
  arena_id?: string;
  arena_title?: string;
  /** The arena's machine filters, so a branch can screen without guessing. */
  arena?: Arena;
}

export interface CompileSeedResult {
  seed: string;
  mode: SeedMode;
  /**
   * Fingerprint of the seed AND what it actually selected (mission + arena
   * ids). Two runs with the same `selection_hash` researched the same thing.
   */
  selection_hash: string;
  taxonomy_version: number;
  arenas_version?: number;
  run_date: string;
  desk_state_hash: string;
  nonce?: string;
  capabilities: DeskCapability[];
  mission?: { family_id: string; card_id: string; title: string };
  arena_ids?: string[];
  cards: SeededCard[];
  deferred: { family_id: string; reason: string; activate_with?: string }[];
  excluded: { family_id: string; card_id: string; reason: string }[];
  paper_only: true;
}

/**
 * Deterministic 32-bit PRNG (mulberry32). Small, dependency-free, and stable
 * across Node versions — which a Math.random-based shuffle would not be.
 */
function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function seededShuffle<T>(items: readonly T[], rng: () => number): T[] {
  const out = [...items];
  for (let i = out.length - 1; i > 0; i -= 1) {
    const j = Math.floor(rng() * (i + 1));
    [out[i], out[j]] = [out[j]!, out[i]!];
  }
  return out;
}

/** Weighted sample without replacement; higher weight is drawn earlier more often. */
function weightedOrder<T extends { weight: number }>(items: readonly T[], rng: () => number): T[] {
  if (items.length <= 1) return [...items];
  const remaining = items.map((item) => ({ item, weight: Math.max(item.weight, 1e-9) }));
  const ordered: T[] = [];
  while (remaining.length > 0) {
    const total = remaining.reduce((sum, row) => sum + row.weight, 0);
    let pick = rng() * total;
    let idx = remaining.length - 1;
    for (let i = 0; i < remaining.length; i += 1) {
      pick -= remaining[i]!.weight;
      if (pick <= 0) {
        idx = i;
        break;
      }
    }
    ordered.push(remaining[idx]!.item);
    remaining.splice(idx, 1);
  }
  return ordered;
}

/** What an unbound card reports, so every card answers the same question. */
const NO_ARENA_LABEL = 'none (this card is not bound to an arena)';

interface EligibilityPass {
  /** family id -> its eligible cards, already weight-ordered for this seed. */
  eligibleByFamily: Map<string, SeededCard[]>;
  /** family id -> the weight of the card it would actually send. */
  topWeightByFamily: Map<string, number>;
  familyById: Map<string, TaxonomyFamily>;
  deferred: CompileSeedResult['deferred'];
  excluded: CompileSeedResult['excluded'];
  reservedFamilies: string[];
}

/**
 * Which cards the desk could run at all today, and why the rest are out. Both
 * seed modes start here, so a capability the desk lacks removes a card from
 * every mode with the same recorded reason.
 */
function eligibilityPass(
  taxonomy: Taxonomy,
  capabilities: DeskCapability[],
  rng: () => number,
): EligibilityPass {
  const held = new Set<string>(capabilities);
  const deferred: CompileSeedResult['deferred'] = [];
  const excluded: CompileSeedResult['excluded'] = [];
  const eligibleByFamily = new Map<string, SeededCard[]>();
  const topWeightByFamily = new Map<string, number>();
  const familyById = new Map<string, TaxonomyFamily>();
  const reservedFamilies: string[] = [];
  for (const family of taxonomy.families) {
    familyById.set(family.id, family);
    if (family.status === 'deferred') {
      deferred.push({
        family_id: family.id,
        reason: family.deferred_reason?.trim() ?? 'family is deferred',
        activate_with: family.requires_capability_to_activate,
      });
      for (const card of family.cards) {
        excluded.push({
          family_id: family.id,
          card_id: card.id,
          reason: `family ${family.id} is deferred`,
        });
      }
      continue;
    }

    const eligible: Array<SeededCard & { weight: number }> = [];
    for (const card of family.cards) {
      const missing = card.requires.filter((requirement) => !held.has(requirement));
      if (missing.length > 0) {
        excluded.push({
          family_id: family.id,
          card_id: card.id,
          reason: `desk lacks capability: ${missing.join(', ')}`,
        });
        continue;
      }
      eligible.push({
        family_id: family.id,
        card_id: card.id,
        title: card.title,
        directive: card.directive.trim(),
        ...(card.deliverable ? { deliverable: card.deliverable.trim() } : {}),
        // live_execution is implicit on every card, whatever the file says.
        prohibits: [...new Set(['live_execution', ...card.prohibits])],
        human_only: family.human_only,
        paper_only: true,
        arena_label: NO_ARENA_LABEL,
        weight: card.weight ?? 1,
      });
    }

    if (eligible.length > 0) {
      const ordered = weightedOrder(eligible, rng);
      eligibleByFamily.set(
        family.id,
        ordered.map(({ weight: _w, ...seeded }) => seeded),
      );
      topWeightByFamily.set(family.id, ordered[0]!.weight);
      if (family.always_reserve_slot) reservedFamilies.push(family.id);
    }
  }

  return { eligibleByFamily, topWeightByFamily, familyById, deferred, excluded, reservedFamilies };
}

/** One card per family, breadth-first across methods. The v2 behaviour. */
function selectStratified(
  pass: EligibilityPass,
  maxCards: number,
  rng: () => number,
): SeededCard[] {
  const { eligibleByFamily, reservedFamilies } = pass;
  const selected: SeededCard[] = [];
  const takenFrom = new Set<string>();

  // Reserved families (explore_seed) get a slot first, so exploration is not
  // crowded out by whichever family happens to have the most cards.
  for (const familyId of reservedFamilies) {
    if (selected.length >= maxCards) break;
    const card = eligibleByFamily.get(familyId)?.[0];
    if (card) {
      selected.push(card);
      takenFrom.add(familyId);
    }
  }

  // Stratify: one card per remaining family before any family repeats.
  const familyOrder = seededShuffle([...eligibleByFamily.keys()], rng);
  for (const familyId of familyOrder) {
    if (selected.length >= maxCards) break;
    if (takenFrom.has(familyId)) continue;
    const card = eligibleByFamily.get(familyId)?.[0];
    if (card) {
      selected.push(card);
      takenFrom.add(familyId);
    }
  }

  // Only then fill any remaining budget with second-and-later cards.
  if (selected.length < maxCards) {
    const remainder = familyOrder.flatMap((familyId) =>
      (eligibleByFamily.get(familyId) ?? []).slice(1),
    );
    for (const card of seededShuffle(remainder, rng)) {
      if (selected.length >= maxCards) break;
      selected.push(card);
    }
  }

  return selected;
}

interface MissionSelection {
  mission: { family_id: string; card_id: string; title: string };
  arena_ids: string[];
  cards: SeededCard[];
}

/** Resolve `--mission`: a family id, or a card id inside a mission family. */
function resolveMissionOverride(
  pass: EligibilityPass,
  override: string,
): { familyId: string; card: SeededCard } {
  const byFamily = pass.eligibleByFamily.get(override);
  if (byFamily && byFamily[0]) return { familyId: override, card: byFamily[0] };

  for (const [familyId, cards] of pass.eligibleByFamily) {
    const card = cards.find((c) => c.card_id === override);
    if (card) return { familyId, card };
  }

  // Naming why it is out beats "unknown mission": the usual cause is a
  // capability the desk lost, not a typo, and those want different fixes.
  const excludedHere = pass.excluded.find(
    (e) => e.card_id === override || e.family_id === override,
  );
  if (excludedHere) {
    throw new TaxonomyError(
      `mission "${override}" is not eligible today: ${excludedHere.reason}`,
      { hint: 'Restore the capability, or pick another mission.' },
    );
  }
  throw new TaxonomyError(`no eligible mission matches "${override}"`, {
    hint: `Eligible mission families: ${missionFamilyIds(pass).join(', ') || 'none'}. Pass a family id or a card id.`,
  });
}

function missionFamilyIds(pass: EligibilityPass): string[] {
  return [...pass.eligibleByFamily.keys()].filter(
    (id) => pass.familyById.get(id)?.mission_eligible === true,
  );
}

/**
 * One mission, N arenas. The branches differ only in where they look, so the
 * morning produces candidates that can actually be ranked against each other
 * instead of four unrelated notes.
 */
function selectMissionXArena(
  pass: EligibilityPass,
  input: CompileSeedInput,
  arenaSet: ArenaSet,
  rng: () => number,
): MissionSelection {
  let missionFamilyId: string;
  let missionCard: SeededCard;

  if (input.mission) {
    const resolved = resolveMissionOverride(pass, input.mission);
    missionFamilyId = resolved.familyId;
    missionCard = resolved.card;
  } else {
    const candidates = missionFamilyIds(pass).map((id) => ({
      id,
      // A family's pull is the pull of the card it would actually send, not the
      // number of cards it happens to list.
      weight: pass.topWeightByFamily.get(id) ?? 1,
    }));
    if (candidates.length === 0) {
      throw new TaxonomyError('no mission-eligible family is available today', {
        hint: 'Mark at least one active family `mission_eligible: true` in taxonomy/cards.yaml, or compile with --mode stratify_families.',
        details: { eligible_families: [...pass.eligibleByFamily.keys()] },
      });
    }
    missionFamilyId = weightedOrder(candidates, rng)[0]!.id;
    missionCard = pass.eligibleByFamily.get(missionFamilyId)![0]!;
  }

  // One slot may go to exploration instead of a fourth arena, so a mission
  // morning can still sample outside whatever the mission is good at.
  const exploreCard = input.includeExplore
    ? (pass.reservedFamilies
        .filter((id) => id !== missionFamilyId)
        .map((id) => pass.eligibleByFamily.get(id)?.[0])
        .find((card): card is SeededCard => card !== undefined) ?? undefined)
    : undefined;

  const arenaBudget = Math.max(1, input.maxCards - (exploreCard ? 1 : 0));
  const arenas = seededShuffle(arenaSet.arenas, rng).slice(
    0,
    Math.min(arenaBudget, arenaSet.arenas.length),
  );

  const cards: SeededCard[] = arenas.map((arena) => ({
    ...missionCard,
    card_id: `${missionCard.card_id}__${arena.id}`,
    title: `${missionCard.title} — ${arena.title}`,
    directive: `${missionCard.directive}\n\n${arenaScopeBlock(arena)}`,
    arena_label: `${arena.id} — ${arena.title}`,
    mission_family_id: missionFamilyId,
    mission_card_id: missionCard.card_id,
    arena_id: arena.id,
    arena_title: arena.title,
    arena,
  }));
  if (exploreCard) cards.push(exploreCard);

  return {
    mission: {
      family_id: missionFamilyId,
      card_id: missionCard.card_id,
      title: missionCard.title,
    },
    arena_ids: arenas.map((a) => a.id),
    cards,
  };
}

export function compileSeed(input: CompileSeedInput): CompileSeedResult {
  for (const capability of input.capabilities) {
    if (!(ALL_CAPABILITIES as readonly string[]).includes(capability)) {
      throw new TaxonomyError(`unknown desk capability: "${capability}"`, {
        hint: `Known capabilities: ${ALL_CAPABILITIES.join(', ')}`,
      });
    }
  }
  if (input.maxCards < 1) {
    throw new TaxonomyError('maxCards must be at least 1', { hint: 'Try --max-cards 4.' });
  }

  const mode = input.mode ?? DEFAULT_SEED_MODE;
  const taxonomy = input.taxonomy ?? loadTaxonomy(input.taxonomyPath);
  const arenaSet =
    mode === 'mission_x_arena'
      ? (input.arenas ?? loadArenas(input.arenasPath ?? defaultArenasPath(input.taxonomyPath)))
      : undefined;

  // Everything that can change what gets picked is in the seed material, so a
  // recorded seed can be replayed against the same taxonomy and arena set.
  const seed = sha256Hex(
    [
      input.runDate,
      input.deskStateHash,
      input.nonce ?? '',
      String(taxonomy.version),
      mode,
      arenaSet ? `arenas:${arenaSet.version}` : '',
      input.mission ?? '',
      input.includeExplore ? 'explore' : '',
    ].join('|'),
  );
  const rng = mulberry32(parseInt(seed.slice(0, 8), 16));

  const pass = eligibilityPass(taxonomy, input.capabilities, rng);
  if (pass.eligibleByFamily.size === 0) {
    throw new TaxonomyError('no directive card is eligible under the current capabilities', {
      hint: `The desk reported capabilities [${input.capabilities.join(', ') || 'none'}]. Every card needs at least polymarket_public_data and local_evidence_store.`,
      details: { excluded: pass.excluded },
    });
  }

  const mission =
    mode === 'mission_x_arena'
      ? selectMissionXArena(pass, input, arenaSet!, rng)
      : undefined;
  const cards = mission ? mission.cards : selectStratified(pass, input.maxCards, rng);

  return {
    seed,
    mode,
    selection_hash: sha256Hex(
      [seed, mode, mission?.mission.card_id ?? '', ...(mission?.arena_ids ?? []), ...cards.map((c) => c.card_id)].join(
        '|',
      ),
    ),
    taxonomy_version: taxonomy.version,
    ...(arenaSet ? { arenas_version: arenaSet.version } : {}),
    run_date: input.runDate,
    desk_state_hash: input.deskStateHash,
    ...(input.nonce ? { nonce: input.nonce } : {}),
    capabilities: input.capabilities,
    ...(mission ? { mission: mission.mission, arena_ids: mission.arena_ids } : {}),
    cards,
    deferred: pass.deferred,
    excluded: pass.excluded,
    paper_only: true,
  };
}

/** arenas.yaml sits beside cards.yaml unless an operator says otherwise. */
function defaultArenasPath(taxonomyPath: string): string {
  const separator = taxonomyPath.includes('\\') && !taxonomyPath.includes('/') ? '\\' : '/';
  const idx = taxonomyPath.lastIndexOf(separator);
  return idx < 0 ? 'arenas.yaml' : `${taxonomyPath.slice(0, idx)}${separator}arenas.yaml`;
}

/**
 * A fingerprint of what the desk currently knows. Feeding this into the seed
 * makes the research plan move as evidence accumulates, rather than repeating
 * the same cards until someone changes the date.
 */
export function deskStateHash(store: DeskStore): string {
  const counts: Record<string, number> = {};
  for (const table of [
    'markets',
    'outcome_tokens',
    'market_observations',
    'source_snapshots',
    'signals',
    'paper_ledger',
  ]) {
    counts[table] = (
      store.db.prepare(`SELECT COUNT(*) AS n FROM ${table}`).get() as { n: number }
    ).n;
  }
  const sources = store.sources.listSources();
  return objectHash({ counts, sources });
}

/** The capabilities this desk actually has, given its configuration. */
export function detectCapabilities(): DeskCapability[] {
  return [
    'polymarket_public_data',
    'primary_source_collection',
    'local_evidence_store',
    'paper_ledger',
  ];
}
