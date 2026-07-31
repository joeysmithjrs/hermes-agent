import { join } from 'node:path';

import { describe, expect, it } from 'vitest';

import { SchemaValidationError, TaxonomyError } from '../src/core/errors.js';
import { arenaScopeBlock, findArena, loadArenas } from '../src/taxonomy/arenas.js';

const ARENAS_PATH = join(import.meta.dirname, '..', 'taxonomy', 'arenas.yaml');

const minimalArena = {
  id: 'pop_culture',
  title: 'Pop culture',
  description: 'A description long enough to pass validation.',
};

describe('arena set', () => {
  it('loads and validates the shipped arenas', () => {
    const set = loadArenas(ARENAS_PATH);
    expect(set.version).toBeGreaterThan(0);
    expect(set.arenas.length).toBeGreaterThanOrEqual(6);
  });

  it('ships the arenas the morning fanout expects', () => {
    const ids = loadArenas(ARENAS_PATH).arenas.map((a) => a.id);
    expect(ids).toEqual(
      expect.arrayContaining([
        'pop_culture',
        'politics',
        'tech',
        'sports',
        'crypto',
        'macro_sched',
      ]),
    );
  });

  it('gives every arena at least one machine filter or an explicit residual scope', () => {
    for (const arena of loadArenas(ARENAS_PATH).arenas) {
      const filters =
        arena.gamma_tag_any.length +
        arena.slug_includes.length +
        arena.slug_excludes.length +
        (arena.question_regex ? 1 : 0);
      expect(filters, `arena ${arena.id} has no machine filter`).toBeGreaterThan(0);
    }
  });

  it('rejects an empty arena file', () => {
    expect(() =>
      loadArenas(ARENAS_PATH, { overrideDocument: { version: 1, arenas: [] } }),
    ).toThrow(SchemaValidationError);
  });

  it('rejects a missing file', () => {
    expect(() => loadArenas(join(import.meta.dirname, 'no-such-arenas.yaml'))).toThrow(
      TaxonomyError,
    );
  });

  it('rejects duplicate arena ids', () => {
    expect(() =>
      loadArenas(ARENAS_PATH, {
        overrideDocument: { version: 1, arenas: [minimalArena, { ...minimalArena }] },
      }),
    ).toThrow(/duplicate arena id/);
  });

  it('rejects an arena id that is not a lowercase slug', () => {
    expect(() =>
      loadArenas(ARENAS_PATH, {
        overrideDocument: { version: 1, arenas: [{ ...minimalArena, id: 'Pop Culture' }] },
      }),
    ).toThrow(SchemaValidationError);
  });

  it('rejects a question_regex that does not compile', () => {
    expect(() =>
      loadArenas(ARENAS_PATH, {
        overrideDocument: { version: 1, arenas: [{ ...minimalArena, question_regex: '([a-' }] },
      }),
    ).toThrow(/question_regex/);
  });

  it('rejects an unknown field rather than silently ignoring it', () => {
    expect(() =>
      loadArenas(ARENAS_PATH, {
        overrideDocument: { version: 1, arenas: [{ ...minimalArena, tags: ['oops'] }] },
      }),
    ).toThrow(SchemaValidationError);
  });

  it('names the known arenas when asked for one that does not exist', () => {
    const set = loadArenas(ARENAS_PATH);
    expect(findArena(set, 'politics').id).toBe('politics');
    expect(() => findArena(set, 'nonsense')).toThrow(/unknown arena/);
  });
});

describe('arena scope block', () => {
  it('states the arena, its filters and the reject-rather-than-wander rule', () => {
    const arena = findArena(loadArenas(ARENAS_PATH), 'macro_sched');
    const block = arenaScopeBlock(arena);
    expect(block).toContain('ARENA SCOPE');
    expect(block).toContain('macro_sched');
    expect(block).toContain('gamma tag is any of');
    expect(block).toContain('question matches');
    expect(block).toMatch(/reject/i);
  });

  it('omits filter lines an arena does not define', () => {
    const block = arenaScopeBlock({
      ...minimalArena,
      gamma_tag_any: [],
      slug_includes: [],
      slug_excludes: [],
      examples: [],
      bans: [],
    });
    expect(block).not.toContain('in-arena test');
    expect(block).not.toContain('out of bounds');
    expect(block).toContain('Stay inside this arena');
  });
});
