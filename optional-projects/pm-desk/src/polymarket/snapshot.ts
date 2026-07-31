import { MarketDataError } from '../core/errors.js';
import { systemClock, type Clock, type IsoTimestamp } from '../core/time.js';
import type {
  BookLevel,
  CapabilityGap,
  OrderBookLike,
  PublicMarketDataClient,
} from './client-interface.js';

export interface TokenSnapshot {
  token_id: string;
  observed_at: IsoTimestamp;
  mid: number | null;
  best_bid: number | null;
  best_ask: number | null;
  spread: number | null;
  last_trade_price: number | null;
  book_available: boolean;
  bid_depth: number | null;
  ask_depth: number | null;
  /** Capabilities this host could not serve for this token, reported explicitly. */
  unavailable: CapabilityGap[];
}

export interface SnapshotOptions {
  clock?: Clock;
  includeLastTrade?: boolean;
}

function num(level: BookLevel, key: 'price' | 'size'): number {
  return typeof level[key] === 'number' ? level[key] : Number(level[key]);
}

/**
 * Best bid is the highest bid and best ask the lowest ask, computed rather than
 * read off an assumed sort order — the API has returned both orderings.
 */
export function summarizeBook(book: OrderBookLike): {
  best_bid: number | null;
  best_ask: number | null;
  spread: number | null;
  bid_depth: number | null;
  ask_depth: number | null;
  available: boolean;
} {
  const bids = (book.bids ?? []).filter((l) => Number.isFinite(num(l, 'price')));
  const asks = (book.asks ?? []).filter((l) => Number.isFinite(num(l, 'price')));

  const best_bid = bids.length > 0 ? Math.max(...bids.map((l) => num(l, 'price'))) : null;
  const best_ask = asks.length > 0 ? Math.min(...asks.map((l) => num(l, 'price'))) : null;
  const spread =
    best_bid !== null && best_ask !== null ? roundProbability(best_ask - best_bid) : null;

  return {
    best_bid,
    best_ask,
    spread,
    bid_depth: bids.length > 0 ? sum(bids.map((l) => num(l, 'size'))) : null,
    ask_depth: asks.length > 0 ? sum(asks.map((l) => num(l, 'size'))) : null,
    // A book with no levels on either side is not a usable book.
    available: best_bid !== null || best_ask !== null,
  };
}

function sum(values: number[]): number {
  return values.reduce((acc, v) => acc + (Number.isFinite(v) ? v : 0), 0);
}

/** Kills float noise like 0.51 - 0.5 = 0.010000000000000009. */
function roundProbability(value: number): number {
  return Math.round(value * 1e6) / 1e6;
}

/**
 * Collects one point-in-time view of a token. Partial failures degrade to nulls
 * plus a `CapabilityGap`; a token with no usable data at all raises rather than
 * writing a snapshot that looks like a real observation of a zero price.
 */
export async function fetchTokenSnapshot(
  client: PublicMarketDataClient,
  tokenId: string,
  options: SnapshotOptions = {},
): Promise<TokenSnapshot> {
  const clock = options.clock ?? systemClock;
  const observed_at = clock.now();
  const unavailable: CapabilityGap[] = [];

  let mid: number | null = null;
  try {
    const value = await client.fetchMidpoint(tokenId);
    mid = Number.isFinite(value) ? value : null;
    if (mid === null) unavailable.push({ capability: 'midpoint', reason: 'non-numeric midpoint' });
  } catch (err) {
    unavailable.push({ capability: 'midpoint', reason: messageOf(err) });
  }

  let bookSummary = {
    best_bid: null as number | null,
    best_ask: null as number | null,
    spread: null as number | null,
    bid_depth: null as number | null,
    ask_depth: null as number | null,
    available: false,
  };
  try {
    bookSummary = summarizeBook(await client.fetchOrderBook(tokenId));
    if (!bookSummary.available) {
      unavailable.push({ capability: 'order_book', reason: 'book has no levels' });
    }
  } catch (err) {
    unavailable.push({ capability: 'order_book', reason: messageOf(err) });
  }

  let last_trade_price: number | null = null;
  if (options.includeLastTrade) {
    try {
      const value = await client.fetchLastTradePrice(tokenId);
      last_trade_price = typeof value === 'number' && Number.isFinite(value) ? value : null;
    } catch (err) {
      unavailable.push({ capability: 'last_trade', reason: messageOf(err) });
    }
  }

  if (mid === null && !bookSummary.available) {
    throw new MarketDataError(`no market data available for token ${tokenId}`, {
      hint: 'The public client could not serve midpoint or book for this token. Check the token id, or retry — the desk will not store a fabricated observation.',
      details: { unavailable },
    });
  }

  return {
    token_id: tokenId,
    observed_at,
    mid,
    best_bid: bookSummary.best_bid,
    best_ask: bookSummary.best_ask,
    spread: bookSummary.spread,
    last_trade_price,
    book_available: bookSummary.available,
    bid_depth: bookSummary.bid_depth,
    ask_depth: bookSummary.ask_depth,
    unavailable,
  };
}

function messageOf(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}
