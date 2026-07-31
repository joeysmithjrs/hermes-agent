import { MarketDataError } from '../core/errors.js';
import { parseIsoToEpochMs, type IsoTimestamp } from '../core/time.js';
import type { MarketUpsert, Outcome, TokenUpsert } from '../store/types.js';

/**
 * Translates official SDK market records into the desk's storage contract.
 *
 * The SDK returns timestamps at second precision (`2026-07-31T12:00:00Z`) while
 * the desk standardises on milliseconds, so every boundary timestamp is upgraded
 * here rather than at each call site.
 */
export function toIsoMs(value: unknown): IsoTimestamp | null {
  if (value === null || value === undefined || value === '') return null;
  if (typeof value === 'number') return new Date(value).toISOString();
  if (typeof value !== 'string') return null;
  try {
    return new Date(parseIsoToEpochMs(value)).toISOString();
  } catch {
    return null;
  }
}

export interface NormalizedMarket {
  market: MarketUpsert;
  tokens: TokenUpsert[];
}

function pick(source: Record<string, unknown>, key: string): unknown {
  return source[key];
}

function asString(value: unknown): string | null {
  if (value === null || value === undefined) return null;
  if (typeof value === 'string') return value.length > 0 ? value : null;
  if (typeof value === 'number' || typeof value === 'bigint') return String(value);
  return null;
}

function asBool(value: unknown, fallback = false): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

export function normalizeMarket(raw: unknown, observedAt: IsoTimestamp): NormalizedMarket {
  if (!raw || typeof raw !== 'object') {
    throw new MarketDataError('market record is not an object', {
      hint: 'The public client returned an unexpected payload shape.',
    });
  }
  const record = raw as Record<string, unknown>;
  const marketId = asString(pick(record, 'id'));
  if (!marketId) {
    throw new MarketDataError('market record has no usable id', {
      hint: 'Every stored market needs a stable id from the official client.',
      details: { keys: Object.keys(record) },
    });
  }

  const state = (pick(record, 'state') ?? {}) as Record<string, unknown>;
  const resolution = (pick(record, 'resolution') ?? {}) as Record<string, unknown>;
  const events = Array.isArray(pick(record, 'events'))
    ? (pick(record, 'events') as Record<string, unknown>[])
    : [];
  const firstEvent = events[0];

  const market: MarketUpsert = {
    market_id: marketId,
    condition_id: asString(pick(record, 'conditionId')),
    question: asString(pick(record, 'question')) ?? asString(pick(record, 'slug')) ?? marketId,
    slug: asString(pick(record, 'slug')),
    category: asString(pick(record, 'category')),
    active: asBool(state.active),
    closed: asBool(state.closed),
    accepting_orders: asBool(state.acceptingOrders),
    neg_risk: asBool(state.negRisk),
    start_date: toIsoMs(state.startDate),
    end_date: toIsoMs(state.endDate),
    resolution_source: asString(resolution.source),
    event_id: firstEvent ? asString(firstEvent.id) : null,
    event_title: firstEvent ? asString(firstEvent.title) : null,
    observed_at: observedAt,
    raw: record,
  };

  const outcomes = (pick(record, 'outcomes') ?? {}) as Record<string, unknown>;
  const tokens: TokenUpsert[] = [];
  for (const [key, outcome] of [
    ['yes', 'YES'],
    ['no', 'NO'],
  ] as const) {
    const entry = outcomes[key];
    if (!entry || typeof entry !== 'object') continue;
    const tokenId = asString((entry as Record<string, unknown>).tokenId);
    if (!tokenId) continue;
    tokens.push({
      token_id: tokenId,
      market_id: marketId,
      outcome: outcome as Outcome,
      label: asString((entry as Record<string, unknown>).label),
      observed_at: observedAt,
    });
  }

  return { market, tokens };
}

/** The indicative price the discovery payload already carries, when present. */
export function indicativePrices(raw: unknown): Record<string, number> {
  if (!raw || typeof raw !== 'object') return {};
  const outcomes = ((raw as Record<string, unknown>).outcomes ?? {}) as Record<string, unknown>;
  const out: Record<string, number> = {};
  for (const key of ['yes', 'no']) {
    const entry = outcomes[key] as Record<string, unknown> | undefined;
    const tokenId = entry ? asString(entry.tokenId) : null;
    const price = entry ? Number(entry.price) : Number.NaN;
    if (tokenId && Number.isFinite(price)) out[tokenId] = price;
  }
  return out;
}
