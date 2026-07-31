/**
 * The narrow read-only surface the desk depends on.
 *
 * Everything downstream of this interface is deterministic code under test with
 * a fake. It exposes discovery, market detail, midpoint, book and history —
 * and deliberately nothing that could place, sign or authorize anything. The
 * real implementation in `client.ts` is the only file that imports the SDK.
 */

export interface MarketDataPage {
  items: Record<string, unknown>[];
  hasMore: boolean;
  nextCursor?: string;
}

export interface BookLevel {
  price: string | number;
  size: string | number;
}

export interface OrderBookLike {
  bids: BookLevel[];
  asks: BookLevel[];
  timestamp?: number;
}

export interface ListMarketsPageRequest {
  pageSize: number;
  cursor?: string;
  closed?: boolean;
  active?: boolean;
  tagId?: number;
  slug?: string;
}

export interface PricePoint {
  t: number;
  p: number;
}

export interface PublicMarketDataClient {
  /** Identifies the transport in logs and observation rows. */
  readonly kind: string;
  /** Whether the underlying client offers a public realtime subscription API. */
  readonly supportsSubscriptions: boolean;

  listMarketsPage(request: ListMarketsPageRequest): Promise<MarketDataPage>;
  fetchMarket(marketId: string): Promise<Record<string, unknown>>;
  fetchMidpoint(tokenId: string): Promise<number>;
  fetchOrderBook(tokenId: string): Promise<OrderBookLike>;
  fetchLastTradePrice(tokenId: string): Promise<number | null>;
  fetchPriceHistory(tokenId: string, interval?: string): Promise<PricePoint[]>;
}

/** A capability the client could not serve on this host, reported not swallowed. */
export interface CapabilityGap {
  capability: 'midpoint' | 'order_book' | 'last_trade' | 'price_history' | 'subscriptions';
  reason: string;
}
