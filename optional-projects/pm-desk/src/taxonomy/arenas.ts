import { readFileSync } from 'node:fs';

import { parse as parseYaml } from 'yaml';
import { z } from 'zod';

import { TaxonomyError } from '../core/errors.js';
import { validate } from '../schema/common.js';

/**
 * Market arenas — the sectors of Polymarket a morning fans out across.
 *
 * An arena is deliberately *not* a strategy. The taxonomy says what kind of
 * research to do; an arena says where on the venue to do it. Keeping them
 * orthogonal is what lets one mission run against several unrelated corners of
 * the book in the same morning and still produce comparable candidates.
 *
 * The filters are machine-usable on purpose. An agent handed only a title
 * drifts back to whatever it read about yesterday; an agent handed tag and slug
 * predicates has to either find a market inside the arena or say honestly that
 * there wasn't one.
 */

const ArenaSchema = z
  .object({
    id: z
      .string()
      .min(1)
      .regex(/^[a-z0-9_]+$/, 'arena ids are lowercase slugs with underscores'),
    title: z.string().min(1),
    description: z.string().min(10),
    /** Gamma tag slugs; a market matching any of them is inside the arena. */
    gamma_tag_any: z.array(z.string().min(1)).default([]),
    /** Substrings, any of which put a market slug inside the arena. */
    slug_includes: z.array(z.string().min(1)).default([]),
    /** Substrings that take a market out of the arena even if it matched. */
    slug_excludes: z.array(z.string().min(1)).default([]),
    /** Optional regex against the question text; always matched case-insensitively. */
    question_regex: z.string().min(1).optional(),
    /** Soft screening guidance for the agent, not a compiler check. */
    min_liquidity_usd: z.number().nonnegative().optional(),
    examples: z.array(z.string().min(1)).default([]),
    bans: z.array(z.string().min(1)).default([]),
  })
  .strict();

const ArenaSetSchema = z
  .object({
    version: z.number().int().positive(),
    arenas: z.array(ArenaSchema).min(1),
  })
  .strict();

export type Arena = z.infer<typeof ArenaSchema>;
export type ArenaSet = z.infer<typeof ArenaSetSchema>;

export interface LoadArenasOptions {
  /** Used by tests to validate a document without writing it to disk. */
  overrideDocument?: unknown;
}

export function loadArenas(path: string, options: LoadArenasOptions = {}): ArenaSet {
  let document = options.overrideDocument;
  if (document === undefined) {
    try {
      document = parseYaml(readFileSync(path, 'utf8'));
    } catch (cause) {
      throw new TaxonomyError(`cannot read the arena set at ${path}`, {
        hint: 'The shipped arenas live at taxonomy/arenas.yaml.',
        cause,
      });
    }
  }

  const set = validate(ArenaSetSchema, document, 'Arenas');

  // Duplicate ids would make a seed ambiguous about which arena a card meant,
  // and the duplicate is invisible in every downstream artifact.
  const seen = new Set<string>();
  for (const arena of set.arenas) {
    if (seen.has(arena.id)) {
      throw new TaxonomyError(`duplicate arena id "${arena.id}"`, {
        hint: 'Arena ids must be unique; a seed records the id, not the title.',
      });
    }
    seen.add(arena.id);

    // A regex that does not compile silently matches nothing at discover time.
    // Compiled with `i` because that is how it is meant to be applied — an
    // author writing `(?i)` (unsupported in JavaScript) gets told so here.
    if (arena.question_regex !== undefined) {
      try {
        new RegExp(arena.question_regex, 'i');
      } catch (cause) {
        throw new TaxonomyError(
          `arena ${arena.id} has an invalid question_regex: ${arena.question_regex}`,
          { hint: 'Write a JavaScript-compatible regular expression.', cause },
        );
      }
    }
  }

  return set;
}

export function findArena(set: ArenaSet, id: string): Arena {
  const arena = set.arenas.find((a) => a.id === id);
  if (!arena) {
    throw new TaxonomyError(`unknown arena: "${id}"`, {
      hint: `Known arenas: ${set.arenas.map((a) => a.id).join(', ')}`,
    });
  }
  return arena;
}

/**
 * The scope block appended to a mission directive. It is prose because the
 * consumer is a model, but every line of it is checkable against the arena
 * definition, and the last paragraph is the one that matters: leaving the arena
 * because it was easier is the failure this whole mode exists to prevent.
 */
export function arenaScopeBlock(arena: Arena): string {
  const lines: string[] = [
    'ARENA SCOPE (hard constraint for this branch)',
    `  arena: ${arena.id} — ${arena.title}`,
    `  ${arena.description.trim().replace(/\s+/g, ' ')}`,
  ];

  const filters: string[] = [];
  if (arena.gamma_tag_any.length > 0) {
    filters.push(`gamma tag is any of [${arena.gamma_tag_any.join(', ')}]`);
  }
  if (arena.slug_includes.length > 0) {
    filters.push(`market slug contains any of [${arena.slug_includes.join(', ')}]`);
  }
  if (arena.slug_excludes.length > 0) {
    filters.push(`market slug contains none of [${arena.slug_excludes.join(', ')}]`);
  }
  if (arena.question_regex) {
    filters.push(`question matches /${arena.question_regex}/`);
  }
  if (filters.length > 0) {
    lines.push(`  in-arena test: ${filters.join('; ')}.`);
  }
  if (arena.min_liquidity_usd !== undefined) {
    lines.push(
      `  soft screen: prefer a book with roughly $${arena.min_liquidity_usd.toLocaleString('en-US')}+ of liquidity.`,
    );
  }
  if (arena.examples.length > 0) {
    lines.push(`  shapes that fit: ${arena.examples.join(' | ')}.`);
  }
  if (arena.bans.length > 0) {
    lines.push(`  out of bounds inside this arena: ${arena.bans.join(' | ')}.`);
  }
  lines.push(
    '  Stay inside this arena. Reach it with pm-desk market discover plus the',
    '  filters above rather than from memory. If nothing in the arena clears the',
    '  bar this morning, return candidate_status "reject", name the arena, and',
    '  say what you screened — a sibling branch already owns the other arenas, so',
    '  substituting an easier market from outside this one destroys the whole',
    '  point of the fanout and is a worse answer than an honest reject.',
  );
  return lines.join('\n');
}
