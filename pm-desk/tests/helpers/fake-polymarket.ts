import type {
  MarketDataPage,
  OrderBookLike,
  PublicMarketDataClient,
} from '../../src/polymarket/client-interface.js';

/**
 * A fake standing in for the official public client. Everything the desk needs
 * from `@polymarket/client` goes through `PublicMarketDataClient`, so unit tests
 * exercise the real adapter code with zero network access.
 */

export function rawMarket(index?: number): Record<string, unknown> {
  const suffix = index === undefined ? '' : `-${index}`;
  return {
    id: index === undefined ? '540817' : `market${suffix}`,
    slug: `new-rhianna-album-before-gta-vi${suffix}`,
    conditionId: index === undefined ? '0xcond540817' : `0xcond${suffix}`,
    question: index === undefined ? 'New Rihanna Album before GTA VI?' : `Question${suffix}?`,
    description: 'This market will resolve to "Yes" if …',
    category: 'Pop Culture',
    state: {
      active: true,
      closed: false,
      archived: false,
      acceptingOrders: true,
      enableOrderBook: true,
      negRisk: false,
      startDate: '2025-05-02T15:48:10.582Z',
      // Second precision, exactly as the SDK returns it.
      endDate: '2026-07-31T12:00:00Z',
    },
    outcomes: {
      yes: { label: 'Yes', tokenId: `tokenYES${suffix}`, price: '0.505' },
      no: { label: 'No', tokenId: `tokenNO${suffix}`, price: '0.495' },
    },
    resolution: {
      questionId: '0xq',
      source: 'https://example.gov/release',
      umaResolutionStatus: null,
    },
    events: [
      {
        id: '23784',
        slug: 'what-will-happen-before-gta-vi',
        title: 'What will happen before GTA VI?',
      },
    ],
    metrics: { volume: '883791.71', liquidity: '12000' },
  };
}

export interface FakeClientOptions {
  marketCount?: number;
  bookUnavailable?: boolean;
  midpointUnavailable?: boolean;
  emptyBook?: boolean;
  supportsSubscribe?: boolean;
  failTokens?: string[];
}

export class FakePublicClient implements PublicMarketDataClient {
  pagesFetched = 0;
  readonly kind = 'fake';

  constructor(private readonly options: FakeClientOptions = {}) {}

  get supportsSubscriptions(): boolean {
    return this.options.supportsSubscribe ?? false;
  }

  async listMarketsPage(request: {
    pageSize: number;
    cursor?: string;
    closed?: boolean;
  }): Promise<MarketDataPage> {
    this.pagesFetched += 1;
    const total = this.options.marketCount ?? 3;
    const offset = request.cursor ? Number(request.cursor) : 0;
    const items: Record<string, unknown>[] = [];
    for (let i = offset; i < Math.min(offset + request.pageSize, total); i += 1) {
      items.push(rawMarket(i));
    }
    const next = offset + items.length;
    return {
      items,
      hasMore: next < total,
      nextCursor: next < total ? String(next) : undefined,
    };
  }

  async fetchMarket(marketId: string): Promise<Record<string, unknown>> {
    const index = Number(marketId.replace(/\D/g, '')) || 0;
    return rawMarket(index);
  }

  private guard(tokenId: string): void {
    if (this.options.failTokens?.includes(tokenId)) {
      throw new Error(`upstream refused token ${tokenId}`);
    }
  }

  async fetchMidpoint(tokenId: string): Promise<number> {
    this.guard(tokenId);
    if (this.options.midpointUnavailable) throw new Error('midpoint unavailable');
    return 0.505;
  }

  async fetchOrderBook(tokenId: string): Promise<OrderBookLike> {
    this.guard(tokenId);
    if (this.options.bookUnavailable) throw new Error('book unavailable');
    if (this.options.emptyBook) return { bids: [], asks: [] };
    return {
      // Deliberately unsorted / SDK-ordered: the adapter must not assume ordering.
      bids: [
        { price: '0.10', size: '100' },
        { price: '0.50', size: '25' },
        { price: '0.49', size: '80' },
      ],
      asks: [
        { price: '0.62', size: '40' },
        { price: '0.51', size: '10' },
        { price: '0.55', size: '30' },
      ],
    };
  }

  async fetchLastTradePrice(tokenId: string): Promise<number | null> {
    this.guard(tokenId);
    return 0.5;
  }

  async fetchPriceHistory(tokenId: string): Promise<{ t: number; p: number }[]> {
    this.guard(tokenId);
    return [
      { t: 1785367269, p: 0.5 },
      { t: 1785453669, p: 0.505 },
    ];
  }
}
