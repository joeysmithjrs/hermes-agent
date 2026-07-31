import { systemClock, type Clock } from '../core/time.js';
import type { PublicMarketDataClient } from './client-interface.js';
import { fetchTokenSnapshot, type TokenSnapshot } from './snapshot.js';

/**
 * Market feed abstraction.
 *
 * The production path is polling: it is deterministic, testable, and cannot
 * silently stall the way a dropped socket can. `describeRealtimeCapability`
 * reports whether the injected client offers the official public subscription
 * API, and `SubscriptionSeam` documents the exact shape a future subscription
 * feed must implement to be swapped in — no agent owns this loop either way.
 */

export interface MarketFeed {
  on(handler: (snapshot: TokenSnapshot) => void): void;
  onError(handler: (error: Error, tokenId: string) => void): void;
  start(): void;
  stop(): void;
  tick(): Promise<void>;
}

export interface RealtimeCapability {
  /** Whether the client exposes official public realtime subscriptions. */
  subscriptions: boolean;
  /** What the desk is using right now. */
  active: 'polling';
  note: string;
}

export function describeRealtimeCapability(client: PublicMarketDataClient): RealtimeCapability {
  const subscriptions = client.supportsSubscriptions;
  return {
    subscriptions,
    active: 'polling',
    note: subscriptions
      ? 'Client exposes public subscriptions; the desk still polls. Implement SubscriptionSeam to switch.'
      : 'Client exposes no public subscription API on this host; polling is the only available path.',
  };
}

/**
 * The contract a subscription-backed feed must satisfy to replace polling.
 * Kept as a type rather than a stub so nothing pretends to be implemented.
 */
export interface SubscriptionSeam {
  subscribe(tokenIds: readonly string[]): Promise<{
    close(): Promise<void>;
    onEvent(handler: (event: { tokenId: string; payload: unknown }) => void): void;
  }>;
}

export interface PollingFeedOptions {
  tokenIds: readonly string[];
  intervalMs: number;
  clock?: Clock;
  includeLastTrade?: boolean;
}

export class PollingMarketFeed implements MarketFeed {
  private handlers: ((snapshot: TokenSnapshot) => void)[] = [];
  private errorHandlers: ((error: Error, tokenId: string) => void)[] = [];
  private timer?: NodeJS.Timeout;
  private stopped = false;
  private inFlight = false;

  constructor(
    private readonly client: PublicMarketDataClient,
    private readonly options: PollingFeedOptions,
  ) {}

  on(handler: (snapshot: TokenSnapshot) => void): void {
    this.handlers.push(handler);
  }

  onError(handler: (error: Error, tokenId: string) => void): void {
    this.errorHandlers.push(handler);
  }

  /**
   * A single sweep over the configured tokens. Exposed so tests (and callers
   * that own their own scheduler, e.g. cron) can drive the feed deterministically
   * without wall-clock timers.
   */
  async tick(): Promise<void> {
    if (this.stopped || this.inFlight) return;
    this.inFlight = true;
    try {
      for (const tokenId of this.options.tokenIds) {
        try {
          const snapshot = await fetchTokenSnapshot(this.client, tokenId, {
            clock: this.options.clock ?? systemClock,
            includeLastTrade: this.options.includeLastTrade ?? false,
          });
          for (const handler of this.handlers) handler(snapshot);
        } catch (err) {
          const error = err instanceof Error ? err : new Error(String(err));
          for (const handler of this.errorHandlers) handler(error, tokenId);
        }
      }
    } finally {
      this.inFlight = false;
    }
  }

  start(): void {
    if (this.timer) return;
    this.stopped = false;
    this.timer = setInterval(() => {
      void this.tick();
    }, this.options.intervalMs);
    this.timer.unref?.();
  }

  stop(): void {
    this.stopped = true;
    if (this.timer) {
      clearInterval(this.timer);
      this.timer = undefined;
    }
  }
}
