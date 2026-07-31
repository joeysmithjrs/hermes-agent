// The ONLY file in this package that imports @polymarket/client.
//
// It imports exactly one symbol — `createPublicClient` — and constructs a
// read-only public client. There is no signer, no credential, no secure client
// and no trading action anywhere in this module or reachable from it.
// tests/guard.test.ts enforces that property across all of src/.
import { createPublicClient } from '@polymarket/client';

import { MarketDataError } from '../core/errors.js';
import type {
  ListMarketsPageRequest,
  MarketDataPage,
  OrderBookLike,
  PricePoint,
  PublicMarketDataClient,
} from './client-interface.js';

type PublicClient = ReturnType<typeof createPublicClient>;

export interface PolymarketClientOptions {
  /** Inject an already-created public client (used by the live smoke). */
  client?: PublicClient;
}

/**
 * Adapts the official public client to the desk's narrow read-only interface.
 * Every method wraps SDK failures in a `MarketDataError` carrying the capability
 * that failed, so an outage is reported as a typed gap rather than a stack trace.
 */
export class PolymarketPublicAdapter implements PublicMarketDataClient {
  readonly kind = 'sdk';
  private readonly client: PublicClient;

  constructor(options: PolymarketClientOptions = {}) {
    this.client = options.client ?? createPublicClient();
  }

  get supportsSubscriptions(): boolean {
    return typeof (this.client as { subscribe?: unknown }).subscribe === 'function';
  }

  async listMarketsPage(request: ListMarketsPageRequest): Promise<MarketDataPage> {
    try {
      const query: Record<string, unknown> = { pageSize: request.pageSize };
      if (request.closed !== undefined) query.closed = request.closed;
      if (request.active !== undefined) query.active = request.active;
      if (request.tagId !== undefined) query.tagId = request.tagId;
      if (request.slug !== undefined) query.slug = request.slug;

      const paginated = this.client.listMarkets(query);
      const page = request.cursor
        ? await paginated.from(request.cursor as never).firstPage()
        : await paginated.firstPage();

      return {
        items: (page.items ?? []) as unknown as Record<string, unknown>[],
        hasMore: Boolean(page.hasMore),
        nextCursor: page.nextCursor as string | undefined,
      };
    } catch (cause) {
      throw new MarketDataError('market discovery failed', {
        hint: 'The public discovery endpoint did not answer. Retry with a smaller --limit, or check network egress from this host.',
        cause,
      });
    }
  }

  async fetchMarket(marketId: string): Promise<Record<string, unknown>> {
    try {
      return (await this.client.fetchMarket({ id: marketId })) as unknown as Record<
        string,
        unknown
      >;
    } catch (cause) {
      throw new MarketDataError(`market ${marketId} could not be fetched`, {
        hint: 'Confirm the market id came from discovery on this same environment.',
        cause,
      });
    }
  }

  async fetchMidpoint(tokenId: string): Promise<number> {
    const value = await this.client.fetchMidpoint({ tokenId });
    const parsed = Number(value);
    if (!Number.isFinite(parsed)) {
      throw new MarketDataError(`midpoint for token ${tokenId} was not numeric`, {
        hint: 'The desk stores NULL rather than guessing a price.',
        details: { value },
      });
    }
    return parsed;
  }

  async fetchOrderBook(tokenId: string): Promise<OrderBookLike> {
    const book = (await this.client.fetchOrderBook({ tokenId })) as unknown as OrderBookLike;
    return { bids: book.bids ?? [], asks: book.asks ?? [], timestamp: book.timestamp };
  }

  async fetchLastTradePrice(tokenId: string): Promise<number | null> {
    const result = (await this.client.fetchLastTradePrice({ tokenId })) as unknown as
      { price?: string | number } | string | number;
    const raw = typeof result === 'object' && result !== null ? result.price : result;
    const parsed = Number(raw);
    return Number.isFinite(parsed) ? parsed : null;
  }

  async fetchPriceHistory(tokenId: string, interval = '1d'): Promise<PricePoint[]> {
    const points = (await this.client.fetchPriceHistory({
      tokenId,
      interval: interval as never,
    })) as unknown as PricePoint[];
    return (points ?? []).filter((p) => Number.isFinite(p?.t) && Number.isFinite(p?.p));
  }
}

/** Factory used by the CLI. Creates a public, credential-free client. */
export function createDeskPublicClient(
  options: PolymarketClientOptions = {},
): PublicMarketDataClient {
  return new PolymarketPublicAdapter(options);
}
