import { mkdtempSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { TaxonomyError } from '../src/core/errors.js';
import {
  ALL_CAPABILITIES,
  compileSeed,
  deskStateHash,
  loadTaxonomy,
  type DeskCapability,
} from '../src/taxonomy/index.js';
import { openStore, type DeskStore } from '../src/store/index.js';

const TAXONOMY_PATH = join(import.meta.dirname, '..', 'taxonomy', 'cards.yaml');

/** The capability set this desk actually has today. */
const DESK_CAPABILITIES: DeskCapability[] = [
  'polymarket_public_data',
  'primary_source_collection',
  'local_evidence_store',
  'paper_ledger',
];

let dir: string;
let store: DeskStore;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'pm-desk-tax-'));
  store = openStore({ home: dir });
});

afterEach(() => {
  store.close();
  rmSync(dir, { recursive: true, force: true });
});

describe('taxonomy file', () => {
  it('loads and validates the shipped card taxonomy', () => {
    const taxonomy = loadTaxonomy(TAXONOMY_PATH);
    expect(taxonomy.version).toBeGreaterThan(0);
    expect(taxonomy.families.length).toBeGreaterThanOrEqual(6);
  });

  it('declares every family the desk charter names', () => {
    const ids = loadTaxonomy(TAXONOMY_PATH).families.map((f) => f.id);
    expect(ids).toEqual(
      expect.arrayContaining([
        'fair_value_research',
        'microstructure_event',
        'intra_poly_coherence',
        'primary_source_sniper',
        'resolution_oracle',
        'tool_and_build_proposals',
        'semantic_basis',
        'wallet_skill',
        'neg_risk_mm',
        'explore_seed',
      ]),
    );
  });

  it('is on taxonomy version 2+', () => {
    expect(loadTaxonomy(TAXONOMY_PATH).version).toBeGreaterThanOrEqual(2);
  });

  it('carries the five primary_source_sniper cards', () => {
    const family = loadTaxonomy(TAXONOMY_PATH).families.find(
      (f) => f.id === 'primary_source_sniper',
    );
    expect(family?.cards.map((c) => c.id)).toEqual([
      'official_statistics_release',
      'court_docket_opinion',
      'election_authority_result',
      'regulator_agency_notice',
      'corporate_ir_filing',
    ]);
  });

  it('includes fair-value and build-proposal families as active', () => {
    const taxonomy = loadTaxonomy(TAXONOMY_PATH);
    const status = (id: string) => taxonomy.families.find((f) => f.id === id)?.status;
    expect(status('fair_value_research')).toBe('active');
    expect(status('tool_and_build_proposals')).toBe('active');
    expect(status('microstructure_event')).toBe('active');
    expect(status('intra_poly_coherence')).toBe('active');
  });

  it('reserves a slot for fair_value_research and explore_seed', () => {
    const taxonomy = loadTaxonomy(TAXONOMY_PATH);
    const reserved = taxonomy.families.filter((f) => f.always_reserve_slot).map((f) => f.id);
    expect(reserved).toEqual(expect.arrayContaining(['fair_value_research', 'explore_seed']));
  });

  it('defers the families the charter says are not yet available', () => {
    const taxonomy = loadTaxonomy(TAXONOMY_PATH);
    const status = (id: string) => taxonomy.families.find((f) => f.id === id)?.status;
    expect(status('semantic_basis')).toBe('deferred');
    expect(status('neg_risk_mm')).toBe('deferred');
  });

  it('marks resolution_oracle research-only with UMA participation human-only', () => {
    const family = loadTaxonomy(TAXONOMY_PATH).families.find((f) => f.id === 'resolution_oracle');
    expect(family?.status).toBe('active');
    expect(family?.human_only).toContain('uma_propose_dispute');
    for (const card of family?.cards ?? []) {
      expect(card.prohibits).toContain('uma_propose_dispute');
    }
  });

  it('marks wallet_skill paper-only and forbids blind copying', () => {
    const family = loadTaxonomy(TAXONOMY_PATH).families.find((f) => f.id === 'wallet_skill');
    expect(family?.paper_only).toBe(true);
    for (const card of family?.cards ?? []) {
      expect(card.prohibits).toContain('blind_copy_execution');
    }
  });

  it('rejects a taxonomy whose card requires an unknown capability', () => {
    expect(() =>
      loadTaxonomy(TAXONOMY_PATH, {
        overrideDocument: {
          version: 1,
          families: [
            {
              id: 'primary_source_sniper',
              status: 'active',
              cards: [
                {
                  id: 'x',
                  title: 'X',
                  requires: ['teleportation'],
                  directive: 'a directive long enough to pass schema validation',
                },
              ],
            },
          ],
        },
      }),
    ).toThrow(TaxonomyError);
  });
});

describe('deterministic seed compilation', () => {
  const baseInput = () => ({
    taxonomyPath: TAXONOMY_PATH,
    runDate: '2026-07-30',
    deskStateHash: 'a'.repeat(64),
    capabilities: DESK_CAPABILITIES,
    maxCards: 4,
  });

  it('returns identical cards for identical input', () => {
    const a = compileSeed(baseInput());
    const b = compileSeed(baseInput());
    expect(a.seed).toBe(b.seed);
    expect(a.cards.map((c) => c.card_id)).toEqual(b.cards.map((c) => c.card_id));
  });

  it('changes selection when the run date changes', () => {
    const a = compileSeed(baseInput());
    const b = compileSeed({ ...baseInput(), runDate: '2026-08-15' });
    expect(a.seed).not.toBe(b.seed);
  });

  it('changes selection when the desk state changes', () => {
    const a = compileSeed(baseInput());
    const b = compileSeed({ ...baseInput(), deskStateHash: 'b'.repeat(64) });
    expect(a.seed).not.toBe(b.seed);
  });

  it('changes selection when an operator supplies a nonce', () => {
    const a = compileSeed(baseInput());
    const b = compileSeed({ ...baseInput(), nonce: 'retry-1' });
    expect(a.seed).not.toBe(b.seed);
  });

  it('is bounded by maxCards', () => {
    for (const maxCards of [1, 2, 3, 6, 50]) {
      const result = compileSeed({ ...baseInput(), maxCards });
      expect(result.cards.length).toBeLessThanOrEqual(maxCards);
      expect(result.cards.length).toBeGreaterThan(0);
    }
  });

  it('never selects a card from a deferred family', () => {
    const result = compileSeed({ ...baseInput(), maxCards: 50 });
    expect(result.cards.some((c) => c.family_id === 'semantic_basis')).toBe(false);
    expect(result.cards.some((c) => c.family_id === 'neg_risk_mm')).toBe(false);
    expect(result.deferred.map((d) => d.family_id)).toEqual(
      expect.arrayContaining(['semantic_basis', 'neg_risk_mm']),
    );
  });

  it('excludes cards whose required capability the desk lacks', () => {
    const withoutSources = compileSeed({
      ...baseInput(),
      maxCards: 50,
      capabilities: DESK_CAPABILITIES.filter((c) => c !== 'primary_source_collection'),
    });
    expect(withoutSources.cards.some((c) => c.family_id === 'primary_source_sniper')).toBe(false);

    const withSources = compileSeed({ ...baseInput(), maxCards: 50 });
    expect(withSources.cards.some((c) => c.family_id === 'primary_source_sniper')).toBe(true);
  });

  it('reports why each excluded card was excluded', () => {
    // Enough capability for explore_seed to stay eligible, so the compiler
    // returns a result whose `excluded` list can be inspected.
    const result = compileSeed({
      ...baseInput(),
      maxCards: 50,
      capabilities: ['polymarket_public_data', 'local_evidence_store'],
    });
    const excluded = result.excluded.find((e) => e.card_id === 'official_statistics_release');
    expect(excluded?.reason).toMatch(/primary_source_collection/);

    const deferredCard = result.excluded.find((e) => e.family_id === 'neg_risk_mm');
    expect(deferredCard?.reason).toMatch(/deferred/);
  });

  it('stratifies across families before repeating one', () => {
    const result = compileSeed({ ...baseInput(), maxCards: 3 });
    const families = result.cards.map((c) => c.family_id);
    expect(new Set(families).size).toBe(families.length);
  });

  it('always reserves a slot for explore_seed when budget allows', () => {
    const result = compileSeed({ ...baseInput(), maxCards: 4 });
    expect(result.cards.some((c) => c.family_id === 'explore_seed')).toBe(true);
  });

  it('also reserves fair_value_research when budget allows', () => {
    const result = compileSeed({ ...baseInput(), maxCards: 4 });
    expect(result.cards.some((c) => c.family_id === 'fair_value_research')).toBe(true);
  });

  it('carries the paper-only invariant onto every emitted card', () => {
    const result = compileSeed({ ...baseInput(), maxCards: 50 });
    expect(result.paper_only).toBe(true);
    for (const card of result.cards) {
      expect(card.paper_only).toBe(true);
      expect(card.prohibits).toContain('live_execution');
    }
  });

  it('fails loudly when no card is eligible rather than emitting an empty plan', () => {
    expect(() => compileSeed({ ...baseInput(), capabilities: [] })).toThrow(TaxonomyError);
  });

  it('accepts only known capability names', () => {
    expect(() =>
      compileSeed({ ...baseInput(), capabilities: ['nonsense' as DeskCapability] }),
    ).toThrow(TaxonomyError);
    expect(ALL_CAPABILITIES).toContain('polymarket_public_data');
  });
});

describe('desk state hash', () => {
  it('is stable for an unchanged store and moves when the store changes', () => {
    const before = deskStateHash(store);
    expect(before).toMatch(/^[0-9a-f]{64}$/);
    expect(deskStateHash(store)).toBe(before);

    store.markets.upsertMarket({
      market_id: 'm1',
      question: 'Q?',
      active: true,
      closed: false,
      accepting_orders: true,
      neg_risk: false,
      observed_at: '2026-07-30T12:00:00.000Z',
      raw: {},
    });
    expect(deskStateHash(store)).not.toBe(before);
  });
});
