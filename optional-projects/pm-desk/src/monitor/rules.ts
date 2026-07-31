import { secondsBetween, type IsoTimestamp } from '../core/time.js';
import type { MonitorSpec } from '../schema/monitor-spec.js';
import type { MarketRef, MarketSnapshot } from '../schema/signal.js';
import type { DeskStore } from '../store/index.js';
import type { ObservationRow, SnapshotRow, TokenBinding } from '../store/types.js';
import { diffExcerpt } from './diff.js';
import { missingData, silent, stale, type RuleResult } from './types.js';

/**
 * Rule predicates. Each one reads stored evidence and returns a verdict.
 *
 * There is no LLM, no network call and no clock read in here — `now` is passed
 * in — which is what makes `--dry-run` byte-for-byte repeatable.
 */

function ageSeconds(at: IsoTimestamp, now: IsoTimestamp): number {
  return secondsBetween(at, now);
}

function snapshotOf(observation: ObservationRow | undefined): MarketSnapshot | null {
  if (!observation) return null;
  return {
    observed_at: observation.observed_at,
    mid: observation.mid,
    best_bid: observation.best_bid,
    best_ask: observation.best_ask,
    spread: observation.spread,
    last_trade_price: observation.last_trade_price,
    book_available: observation.book_available,
  };
}

function marketRefFrom(
  binding: TokenBinding | undefined,
  fallback: { token_id: string; market_id?: string; condition_id?: string; outcome?: 'YES' | 'NO' },
): MarketRef[] {
  if (binding) {
    return [
      {
        market_id: binding.market_id,
        condition_id: binding.condition_id ?? undefined,
        token_id: binding.token_id,
        outcome: binding.outcome,
        question: binding.question,
        end_date: binding.end_date ?? undefined,
      },
    ];
  }
  if (!fallback.market_id) return [];
  return [
    {
      market_id: fallback.market_id,
      condition_id: fallback.condition_id,
      token_id: fallback.token_id,
      outcome: fallback.outcome,
    },
  ];
}

function sourceRefFrom(current: SnapshotRow, previous: SnapshotRow | undefined) {
  return {
    source_id: current.source_id,
    url: current.url,
    previous_hash: previous?.content_hash ?? current.previous_hash ?? null,
    current_hash: current.content_hash,
    artifact_ref: current.normalized_artifact_ref,
    spec_version: current.spec_version,
    collected_at: current.collected_at,
  };
}

export function evaluateRule(store: DeskStore, spec: MonitorSpec, now: IsoTimestamp): RuleResult {
  switch (spec.kind) {
    case 'primary_source_change':
      return primarySourceChange(store, spec, now);
    case 'market_move':
      return marketMove(store, spec, now);
    case 'spread_widening':
      return spreadWidening(store, spec, now);
    case 'source_market_divergence':
      return sourceMarketDivergence(store, spec, now);
  }
}

function primarySourceChange(
  store: DeskStore,
  spec: Extract<MonitorSpec, { kind: 'primary_source_change' }>,
  now: IsoTimestamp,
): RuleResult {
  const { source_id, max_staleness_s, min_diff_chars, market_refs } = spec.params;

  const current = store.sources.latest(source_id);
  if (!current) {
    return missingData(`no snapshot stored for source ${source_id}`);
  }
  const age = ageSeconds(current.collected_at, now);
  if (age > max_staleness_s) {
    return stale(
      `latest snapshot for ${source_id} is ${Math.round(age)}s old (max_staleness_s=${max_staleness_s})`,
    );
  }
  if (!current.changed) {
    return silent(
      `source ${source_id} fingerprint unchanged (${current.content_hash.slice(0, 12)})`,
    );
  }

  const previous = store.sources.previousChanged(source_id);
  const previousText = previous ? store.artifacts.read(previous.normalized_artifact_ref) : null;
  const currentText = store.artifacts.read(current.normalized_artifact_ref);
  const excerpt = diffExcerpt(previousText, currentText);

  const changedChars = Math.abs(currentText.length - (previousText?.length ?? 0));
  const differingChars = previousText
    ? countDifferingChars(previousText, currentText)
    : currentText.length;
  if (Math.max(changedChars, differingChars) < min_diff_chars) {
    return silent(
      `change smaller than min_diff_chars=${min_diff_chars} (${differingChars} differing chars)`,
    );
  }

  return {
    fired: true,
    candidate: {
      kind: 'primary_source_change',
      observed_at: current.collected_at,
      dedupe_key: `${spec.id}:${spec.version}:${current.content_hash}`,
      market_refs: market_refs.flatMap((binding) =>
        marketRefFrom(store.markets.getTokenBinding(binding.token_id), binding),
      ),
      source_refs: [sourceRefFrom(current, previous)],
      market_snapshot: null,
      evidence: {
        diff_excerpt: excerpt,
        claims: [
          `Primary source ${source_id} changed at ${current.collected_at}.`,
          previous
            ? `Fingerprint ${previous.content_hash.slice(0, 12)} → ${current.content_hash.slice(0, 12)}.`
            : `First recorded snapshot; fingerprint ${current.content_hash.slice(0, 12)}.`,
          ...Object.entries(current.fields)
            .filter(([, value]) => value !== null)
            .map(([field, value]) => `${field}: ${value}`),
        ],
        metrics: { differing_chars: differingChars, text_length: currentText.length },
      },
    },
  };
}

function countDifferingChars(a: string, b: string): number {
  let head = 0;
  while (head < a.length && head < b.length && a[head] === b[head]) head += 1;
  let tail = 0;
  while (
    tail < a.length - head &&
    tail < b.length - head &&
    a[a.length - 1 - tail] === b[b.length - 1 - tail]
  ) {
    tail += 1;
  }
  return Math.max(a.length - head - tail, b.length - head - tail);
}

function marketMove(
  store: DeskStore,
  spec: Extract<MonitorSpec, { kind: 'market_move' }>,
  now: IsoTimestamp,
): RuleResult {
  const { token_id, abs_move, rel_move, lookback_s, max_staleness_s } = spec.params;

  const latest = store.observations.latest(token_id);
  if (!latest) return missingData(`no observations stored for token ${token_id}`);

  const age = ageSeconds(latest.observed_at, now);
  if (age > max_staleness_s) {
    return stale(
      `latest observation for ${token_id} is ${Math.round(age)}s old (max_staleness_s=${max_staleness_s})`,
    );
  }
  if (latest.mid === null) {
    return missingData(`latest observation for ${token_id} has no midpoint`);
  }

  const boundary = new Date(Date.parse(now) - lookback_s * 1000).toISOString();
  const reference = store.observations.latestAtOrBefore(token_id, boundary);
  if (!reference || reference.mid === null) {
    return missingData(
      `no reference observation with a midpoint at or before ${boundary} (lookback_s=${lookback_s})`,
    );
  }
  if (reference.id === latest.id) {
    return silent('latest observation is also the reference; no interval to compare');
  }

  const absMove = Math.abs(latest.mid - reference.mid);
  const relMove = reference.mid === 0 ? Number.POSITIVE_INFINITY : absMove / reference.mid;

  const absFired = abs_move !== undefined && absMove >= abs_move;
  const relFired = rel_move !== undefined && relMove >= rel_move;
  if (!absFired && !relFired) {
    return silent(`move ${absMove.toFixed(4)} abs / ${relMove.toFixed(4)} rel below threshold`);
  }

  const binding = store.markets.getTokenBinding(token_id);
  return {
    fired: true,
    candidate: {
      kind: 'market_move',
      observed_at: latest.observed_at,
      // Keyed on the observation pair, so re-evaluating the same facts is a duplicate.
      dedupe_key: `${spec.id}:${spec.version}:${token_id}:${reference.observed_at}:${latest.observed_at}`,
      market_refs: marketRefFrom(binding, {
        token_id,
        market_id: spec.params.market_id,
        condition_id: spec.params.condition_id,
        outcome: spec.params.outcome,
      }),
      source_refs: [],
      market_snapshot: snapshotOf(latest),
      evidence: {
        claims: [
          `Midpoint moved ${reference.mid.toFixed(4)} → ${latest.mid.toFixed(4)} between ${reference.observed_at} and ${latest.observed_at}.`,
          `Absolute move ${absMove.toFixed(4)} (threshold ${abs_move ?? 'n/a'}), relative ${(relMove * 100).toFixed(1)}% (threshold ${rel_move !== undefined ? `${rel_move * 100}%` : 'n/a'}).`,
        ],
        metrics: {
          abs_move: absMove,
          rel_move: relMove,
          reference_mid: reference.mid,
          latest_mid: latest.mid,
        },
      },
    },
  };
}

function spreadWidening(
  store: DeskStore,
  spec: Extract<MonitorSpec, { kind: 'spread_widening' }>,
  now: IsoTimestamp,
): RuleResult {
  const { token_id, max_spread, alert_on_missing_book, max_staleness_s } = spec.params;

  const latest = store.observations.latest(token_id);
  if (!latest) return missingData(`no observations stored for token ${token_id}`);

  const age = ageSeconds(latest.observed_at, now);
  if (age > max_staleness_s) {
    return stale(`latest observation for ${token_id} is ${Math.round(age)}s old`);
  }

  const binding = store.markets.getTokenBinding(token_id);
  const refs = marketRefFrom(binding, {
    token_id,
    market_id: spec.params.market_id,
    outcome: spec.params.outcome,
  });

  if (!latest.book_available || latest.spread === null) {
    if (!alert_on_missing_book) {
      return silent(`book unavailable for ${token_id} and alert_on_missing_book is off`);
    }
    return {
      fired: true,
      candidate: {
        kind: 'market_move',
        observed_at: latest.observed_at,
        // State-transition key: repeated polls of the same outage are duplicates.
        dedupe_key: `${spec.id}:${spec.version}:${token_id}:missing_book`,
        market_refs: refs,
        source_refs: [],
        market_snapshot: snapshotOf(latest),
        evidence: {
          claims: [
            `Order book unavailable for token ${token_id} as of ${latest.observed_at}.`,
            'This is a data-quality alarm, not a price move: the desk cannot see the book.',
          ],
          metrics: { observed_age_s: Math.round(age) },
        },
      },
    };
  }

  if (latest.spread < max_spread) {
    return silent(`spread ${latest.spread.toFixed(4)} below max_spread=${max_spread}`);
  }

  return {
    fired: true,
    candidate: {
      kind: 'market_move',
      observed_at: latest.observed_at,
      // Bucketed so a drifting-but-still-wide spread does not re-alert every poll.
      dedupe_key: `${spec.id}:${spec.version}:${token_id}:wide_${latest.spread.toFixed(2)}`,
      market_refs: refs,
      source_refs: [],
      market_snapshot: snapshotOf(latest),
      evidence: {
        claims: [
          `Spread widened to ${latest.spread.toFixed(4)} (threshold ${max_spread}) at ${latest.observed_at}.`,
          `Best bid ${latest.best_bid ?? 'n/a'} / best ask ${latest.best_ask ?? 'n/a'}.`,
        ],
        metrics: { spread: latest.spread, max_spread },
      },
    },
  };
}

function sourceMarketDivergence(
  store: DeskStore,
  spec: Extract<MonitorSpec, { kind: 'source_market_divergence' }>,
  now: IsoTimestamp,
): RuleResult {
  const { source_id, market, horizon_s, require_active, max_staleness_s } = spec.params;

  const current = store.sources.latest(source_id);
  if (!current) return missingData(`no snapshot stored for source ${source_id}`);

  const age = ageSeconds(current.collected_at, now);
  if (age > max_staleness_s) {
    return stale(`latest snapshot for ${source_id} is ${Math.round(age)}s old`);
  }
  if (!current.changed) {
    return silent(`source ${source_id} unchanged; nothing to reconcile against the market`);
  }

  const binding = store.markets.getTokenBinding(market.token_id);
  if (!binding) {
    return missingData(
      `token ${market.token_id} is not in the store; run \`pm-desk market discover\` first`,
    );
  }
  if (require_active && (binding.closed || !binding.active)) {
    return silent(`market ${binding.market_id} is closed or not active`);
  }
  if (!binding.end_date) {
    return silent(`market ${binding.market_id} has no end date, so the horizon cannot be checked`);
  }
  const secondsToEnd = secondsBetween(now, binding.end_date);
  if (secondsToEnd < 0) {
    return silent(`market ${binding.market_id} already passed its end date`);
  }
  if (secondsToEnd > horizon_s) {
    return silent(
      `market ${binding.market_id} resolves in ${Math.round(secondsToEnd)}s, beyond horizon_s=${horizon_s}`,
    );
  }

  const previous = store.sources.previousChanged(source_id);
  const previousText = previous ? store.artifacts.read(previous.normalized_artifact_ref) : null;
  const currentText = store.artifacts.read(current.normalized_artifact_ref);
  const observation = store.observations.latest(market.token_id);

  return {
    fired: true,
    candidate: {
      kind: 'source_market_divergence',
      observed_at: current.collected_at,
      dedupe_key: `${spec.id}:${spec.version}:${current.content_hash}:${market.token_id}`,
      market_refs: marketRefFrom(binding, market),
      source_refs: [sourceRefFrom(current, previous)],
      market_snapshot: snapshotOf(observation),
      evidence: {
        diff_excerpt: diffExcerpt(previousText, currentText),
        claims: [
          `Linked source ${source_id} changed at ${current.collected_at} while market ${binding.market_id} is still live.`,
          `Market resolves at ${binding.end_date} (in ${Math.round(secondsToEnd / 3600)}h), within the ${Math.round(horizon_s / 3600)}h horizon.`,
          observation?.mid !== null && observation?.mid !== undefined
            ? `Market midpoint at ${observation.observed_at} was ${observation.mid.toFixed(4)}.`
            : 'No market observation has been taken since the source changed.',
        ],
        metrics: { seconds_to_resolution: Math.round(secondsToEnd) },
      },
    },
  };
}
