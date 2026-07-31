import { systemClock, type Clock } from '../core/time.js';
import type { DeskStore } from '../store/index.js';
import type { PublicMarketDataClient } from './client-interface.js';
import { normalizeMarket } from './normalize.js';
import { fetchTokenSnapshot } from './snapshot.js';

export interface CollectMarketsOptions {
  /** Hard cap on markets fetched. Paging stops as soon as it is reached. */
  limit?: number;
  pageSize?: number;
  closed?: boolean;
  dryRun?: boolean;
  clock?: Clock;
}

export interface CollectMarketsResult {
  markets: number;
  tokens: number;
  pages: number;
  dryRun: boolean;
  skipped: { reason: string; detail: string }[];
  sample: { market_id: string; question: string; token_ids: string[] }[];
}

/**
 * Bounded discovery. `limit` is enforced against the accumulated count *and*
 * used to size the last page request, so a `--limit 5` never pulls 500 records
 * from a public endpoint.
 */
export async function collectMarkets(
  store: DeskStore,
  client: PublicMarketDataClient,
  options: CollectMarketsOptions = {},
): Promise<CollectMarketsResult> {
  const clock = options.clock ?? systemClock;
  const limit = Math.max(1, options.limit ?? 25);
  const pageSize = Math.min(Math.max(1, options.pageSize ?? 50), limit);
  const dryRun = options.dryRun ?? false;

  const skipped: CollectMarketsResult['skipped'] = [];
  const sample: CollectMarketsResult['sample'] = [];
  let cursor: string | undefined;
  let markets = 0;
  let tokens = 0;
  let pages = 0;

  while (markets < limit) {
    const remaining = limit - markets;
    const page = await client.listMarketsPage({
      pageSize: Math.min(pageSize, remaining),
      cursor,
      closed: options.closed ?? false,
    });
    pages += 1;

    for (const raw of page.items) {
      if (markets >= limit) break;
      const observedAt = clock.now();
      let normalized;
      try {
        normalized = normalizeMarket(raw, observedAt);
      } catch (err) {
        skipped.push({
          reason: 'normalization_failed',
          detail: err instanceof Error ? err.message : String(err),
        });
        continue;
      }

      if (!dryRun) {
        store.transaction(() => {
          store.markets.upsertMarket(normalized.market);
          for (const token of normalized.tokens) store.markets.upsertToken(token);
        });
      }
      markets += 1;
      tokens += normalized.tokens.length;
      if (sample.length < 5) {
        sample.push({
          market_id: normalized.market.market_id,
          question: normalized.market.question,
          token_ids: normalized.tokens.map((t) => t.token_id),
        });
      }
    }

    if (!page.hasMore || !page.nextCursor || page.items.length === 0) break;
    cursor = page.nextCursor;
  }

  return { markets, tokens, pages, dryRun, skipped, sample };
}

export interface SnapshotTokensOptions {
  clock?: Clock;
  dryRun?: boolean;
  includeLastTrade?: boolean;
}

export interface SnapshotTokensResult {
  observed: number;
  dryRun: boolean;
  failed: { token_id: string; error: string }[];
  gaps: { token_id: string; capability: string; reason: string }[];
  snapshots: { token_id: string; observed_at: string; mid: number | null; spread: number | null }[];
}

/**
 * One observation per token. A token that fails is recorded and the sweep
 * continues — a single dead token must not blind the whole desk.
 */
export async function snapshotTokens(
  store: DeskStore,
  client: PublicMarketDataClient,
  tokenIds: readonly string[],
  options: SnapshotTokensOptions = {},
): Promise<SnapshotTokensResult> {
  const clock = options.clock ?? systemClock;
  const failed: SnapshotTokensResult['failed'] = [];
  const gaps: SnapshotTokensResult['gaps'] = [];
  const snapshots: SnapshotTokensResult['snapshots'] = [];
  let observed = 0;

  for (const tokenId of tokenIds) {
    try {
      const snapshot = await fetchTokenSnapshot(client, tokenId, {
        clock,
        includeLastTrade: options.includeLastTrade ?? false,
      });
      for (const gap of snapshot.unavailable) {
        gaps.push({ token_id: tokenId, capability: gap.capability, reason: gap.reason });
      }
      if (!options.dryRun) {
        const binding = store.markets.getTokenBinding(tokenId);
        store.observations.append({
          token_id: tokenId,
          market_id: binding?.market_id ?? null,
          observed_at: snapshot.observed_at,
          mid: snapshot.mid,
          best_bid: snapshot.best_bid,
          best_ask: snapshot.best_ask,
          spread: snapshot.spread,
          last_trade_price: snapshot.last_trade_price,
          book_available: snapshot.book_available,
          bid_depth: snapshot.bid_depth,
          ask_depth: snapshot.ask_depth,
          source: `polymarket_public_${client.kind}`,
        });
      }
      observed += 1;
      snapshots.push({
        token_id: tokenId,
        observed_at: snapshot.observed_at,
        mid: snapshot.mid,
        spread: snapshot.spread,
      });
    } catch (err) {
      failed.push({ token_id: tokenId, error: err instanceof Error ? err.message : String(err) });
    }
  }

  return { observed, dryRun: options.dryRun ?? false, failed, gaps, snapshots };
}
