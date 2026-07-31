import { readFileSync } from 'node:fs';

import { parse as parseYaml } from 'yaml';
import { z } from 'zod';

import { TaxonomyError } from '../core/errors.js';
import { objectHash, sha256Hex } from '../core/hash.js';
import { validate } from '../schema/common.js';
import type { DeskStore } from '../store/index.js';

/**
 * Directive taxonomy and its deterministic stratified seed compiler.
 *
 * The point of determinism here is auditability: given a run date, a desk-state
 * hash and an optional nonce, the same cards come out every time, so a recorded
 * research plan can be reproduced and argued with later.
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
}

export interface CompileSeedResult {
  seed: string;
  taxonomy_version: number;
  run_date: string;
  desk_state_hash: string;
  nonce?: string;
  capabilities: DeskCapability[];
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

  const taxonomy = input.taxonomy ?? loadTaxonomy(input.taxonomyPath);
  const seed = sha256Hex(
    [input.runDate, input.deskStateHash, input.nonce ?? '', String(taxonomy.version)].join('|'),
  );
  const rng = mulberry32(parseInt(seed.slice(0, 8), 16));

  const held = new Set<string>(input.capabilities);
  const deferred: CompileSeedResult['deferred'] = [];
  const excluded: CompileSeedResult['excluded'] = [];
  const eligibleByFamily = new Map<string, SeededCard[]>();
  const reservedFamilies: string[] = [];
  for (const family of taxonomy.families) {
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
        weight: card.weight ?? 1,
      });
    }

    if (eligible.length > 0) {
      const ordered = weightedOrder(eligible, rng);
      eligibleByFamily.set(
        family.id,
        ordered.map(({ weight: _w, ...seeded }) => seeded),
      );
      if (family.always_reserve_slot) reservedFamilies.push(family.id);
    }
  }

  if (eligibleByFamily.size === 0) {
    throw new TaxonomyError('no directive card is eligible under the current capabilities', {
      hint: `The desk reported capabilities [${input.capabilities.join(', ') || 'none'}]. Every card needs at least polymarket_public_data and local_evidence_store.`,
      details: { excluded },
    });
  }

  const selected: SeededCard[] = [];
  const takenFrom = new Set<string>();

  // Reserved families (explore_seed) get a slot first, so exploration is not
  // crowded out by whichever family happens to have the most cards.
  for (const familyId of reservedFamilies) {
    if (selected.length >= input.maxCards) break;
    const card = eligibleByFamily.get(familyId)?.[0];
    if (card) {
      selected.push(card);
      takenFrom.add(familyId);
    }
  }

  // Stratify: one card per remaining family before any family repeats.
  const familyOrder = seededShuffle([...eligibleByFamily.keys()], rng);
  for (const familyId of familyOrder) {
    if (selected.length >= input.maxCards) break;
    if (takenFrom.has(familyId)) continue;
    const card = eligibleByFamily.get(familyId)?.[0];
    if (card) {
      selected.push(card);
      takenFrom.add(familyId);
    }
  }

  // Only then fill any remaining budget with second-and-later cards.
  if (selected.length < input.maxCards) {
    const remainder = familyOrder.flatMap((familyId) =>
      (eligibleByFamily.get(familyId) ?? []).slice(1),
    );
    for (const card of seededShuffle(remainder, rng)) {
      if (selected.length >= input.maxCards) break;
      selected.push(card);
    }
  }

  return {
    seed,
    taxonomy_version: taxonomy.version,
    run_date: input.runDate,
    desk_state_hash: input.deskStateHash,
    ...(input.nonce ? { nonce: input.nonce } : {}),
    capabilities: input.capabilities,
    cards: selected,
    deferred,
    excluded,
    paper_only: true,
  };
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
