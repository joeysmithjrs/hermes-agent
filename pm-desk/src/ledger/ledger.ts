import { LedgerInvariantError } from '../core/errors.js';
import { sha256Hex, normalizeText } from '../core/hash.js';
import { randomId } from '../core/ids.js';
import { parseIsoToEpochMs, systemClock, type Clock } from '../core/time.js';
import type { Adjudication } from '../schema/adjudication.js';
import type { MarketSnapshot, SignalEnvelope } from '../schema/signal.js';
import type { DeskStore } from '../store/index.js';
import type { PaperLedgerEntry, SlippageRule } from './types.js';

export interface RecordOptions {
  clock?: Clock;
}

/**
 * Derives the price a paper entry is *assumed* to have been struck at.
 *
 * This is a bookkeeping assumption, never a fill: no order exists, nothing was
 * sent anywhere. If the snapshot lacks the input a rule needs, we refuse rather
 * than substituting a nearby number — a ledger whose entry price is quietly
 * invented is worse than no ledger.
 */
export function assumedEntryPrice(
  snapshot: Pick<MarketSnapshot, 'mid' | 'best_bid' | 'best_ask'> | null,
  rule: SlippageRule,
  outcome: 'YES' | 'NO',
): number {
  const missing = (needed: string, hint: string): never => {
    throw new LedgerInvariantError(
      `slippage_rule ${rule} needs ${needed}, which this entry observation does not have`,
      { hint },
    );
  };

  // No snapshot at all is a different problem from a snapshot missing one side
  // of the book, and suggesting a cheaper slippage rule would be useless advice.
  if (!snapshot) {
    return missing(
      'a market snapshot',
      'This signal carries no market observation, so no slippage rule can price it. Bind the monitor to a market token and take a snapshot (`pm-desk market snapshot --token <id>`) before an adjudication can produce a ledger entry.',
    );
  }

  const bookHint =
    'Take a fresh snapshot (`pm-desk market snapshot --token <id>`) so the book is recorded, or use mid_no_slippage if only a midpoint is available.';

  switch (rule) {
    case 'cross_spread_full': {
      // Buying YES lifts the ask; buying NO is equivalent to hitting the YES bid.
      const price = outcome === 'YES' ? snapshot.best_ask : snapshot.best_bid;
      if (price === null || price === undefined) {
        return missing(outcome === 'YES' ? 'a best ask' : 'a best bid', bookHint);
      }
      return price;
    }
    case 'mid_plus_1_tick': {
      if (snapshot.mid === null || snapshot.mid === undefined)
        return missing('a midpoint', bookHint);
      // One tick is 0.01 on Polymarket's standard tick size.
      return Math.min(1, Math.round((snapshot.mid + 0.01) * 1e6) / 1e6);
    }
    case 'mid_no_slippage': {
      if (snapshot.mid === null || snapshot.mid === undefined) {
        return missing(
          'a midpoint',
          'The recorded observation has no midpoint — the market data endpoint did not answer at observation time. Take a fresh snapshot before recording.',
        );
      }
      return snapshot.mid;
    }
  }
}

export function thesisHash(thesis: string): string {
  return sha256Hex(normalizeText(thesis));
}

/**
 * The only automated path into the ledger. It requires a *validated*
 * `paper_alert` adjudication whose signal is already in this store, so an entry
 * can never exist without the evidence chain that justifies it.
 */
export function recordFromAdjudication(
  store: DeskStore,
  adjudication: Adjudication,
  options: RecordOptions = {},
): PaperLedgerEntry {
  const clock = options.clock ?? systemClock;

  if (adjudication.decision !== 'paper_alert') {
    throw new LedgerInvariantError(
      `only a paper_alert adjudication can create a ledger entry, got "${adjudication.decision}"`,
      {
        hint: 'ignore and watch decisions are recorded as adjudications but never as ledger rows.',
      },
    );
  }

  const proposal = adjudication.ledger_proposal;
  if (!proposal) {
    throw new LedgerInvariantError('a paper_alert must carry a ledger_proposal', {
      hint: 'The adjudication workflow must emit thesis, candidate_outcome, assumed_size_usd, slippage_rule, expiry and invalidations.',
    });
  }

  const stored = store.signals.get(adjudication.signal_id);
  if (!stored) {
    throw new LedgerInvariantError(
      `signal ${adjudication.signal_id} is not in this store, so its evidence cannot be verified`,
      {
        hint: 'Submit the signal through the ingress first, or point --home at the store that recorded it.',
      },
    );
  }

  if (store.ledger.findBySignal(adjudication.signal_id)) {
    throw new LedgerInvariantError(
      `signal ${adjudication.signal_id} already has a paper ledger entry`,
      {
        hint: 'Add a follow-up with `pm-desk ledger annotate` instead of recording a second entry.',
      },
    );
  }

  const signal: SignalEnvelope = stored.envelope;
  const marketRef = signal.market_refs[0];
  const snapshot = signal.market_snapshot ?? null;
  const decided_at = clock.now();

  const entry: PaperLedgerEntry = {
    entry_id: randomId('led'),
    signal_id: signal.signal_id,
    origin: 'adjudication',
    thesis: proposal.thesis,
    thesis_hash: thesisHash(proposal.thesis),
    decided_at,
    market_id: marketRef?.market_id ?? null,
    condition_id: marketRef?.condition_id ?? null,
    token_id: marketRef?.token_id ?? null,
    outcome: proposal.candidate_outcome,
    entry_observed_at: snapshot?.observed_at ?? null,
    entry_mid: snapshot?.mid ?? null,
    entry_best_bid: snapshot?.best_bid ?? null,
    entry_best_ask: snapshot?.best_ask ?? null,
    entry_spread: snapshot?.spread ?? null,
    assumed_size_usd: proposal.assumed_size_usd,
    slippage_rule: proposal.slippage_rule,
    assumed_entry_price: assumedEntryPrice(
      snapshot,
      proposal.slippage_rule,
      proposal.candidate_outcome,
    ),
    expiry_horizon_s: proposal.expiry_horizon_s,
    expires_at: new Date(
      parseIsoToEpochMs(decided_at) + proposal.expiry_horizon_s * 1000,
    ).toISOString(),
    markout_horizons_s: proposal.markout_horizons_s,
    invalidations: [...proposal.invalidations, adjudication.invalidation],
    evidence_refs: [
      ...signal.source_refs.map((ref) => ref.artifact_ref),
      ...signal.source_refs.map((ref) => ref.url),
    ],
    paper_only: true,
    fill_type: 'SIMULATED_NO_FILL',
    created_at: decided_at,
  };

  return store.transaction(() => {
    store.adjudications.record(adjudication, decided_at);
    return store.ledger.insert(entry);
  });
}

export interface ManualEntryInput {
  thesis: string;
  market_id?: string;
  condition_id?: string;
  token_id?: string;
  outcome: 'YES' | 'NO';
  assumed_size_usd: number;
  slippage_rule: SlippageRule;
  expiry_horizon_s: number;
  markout_horizons_s: number[];
  invalidations: string[];
  entry_mid?: number | null;
  entry_best_bid?: number | null;
  entry_best_ask?: number | null;
  entry_spread?: number | null;
  entry_observed_at?: string | null;
  /** The operator must say so explicitly. Defaults to false. */
  acknowledged: boolean;
}

/**
 * The manual escape hatch, for an operator recording a paper thesis that no
 * signal produced. Deliberately requires an explicit acknowledgement so it
 * cannot be reached by an automated caller that merely forgot a field.
 */
export function recordManualEntry(
  store: DeskStore,
  input: ManualEntryInput,
  options: RecordOptions = {},
): PaperLedgerEntry {
  if (!input.acknowledged) {
    throw new LedgerInvariantError(
      'a manual ledger entry needs an explicit operator acknowledgement',
      {
        hint: 'Pass --i-am-recording-a-paper-entry-manually. This path bypasses signal provenance, so it is opt-in by design.',
      },
    );
  }
  if (input.thesis.trim().length < 10) {
    throw new LedgerInvariantError('a manual entry needs a thesis of at least 10 characters', {
      hint: 'The thesis is what a later markout is judged against; make it specific.',
    });
  }

  const clock = options.clock ?? systemClock;
  const decided_at = clock.now();
  const snapshot = {
    mid: input.entry_mid ?? null,
    best_bid: input.entry_best_bid ?? null,
    best_ask: input.entry_best_ask ?? null,
  };

  const entry: PaperLedgerEntry = {
    entry_id: randomId('led'),
    signal_id: null,
    origin: 'manual_operator',
    thesis: input.thesis,
    thesis_hash: thesisHash(input.thesis),
    decided_at,
    market_id: input.market_id ?? null,
    condition_id: input.condition_id ?? null,
    token_id: input.token_id ?? null,
    outcome: input.outcome,
    entry_observed_at: input.entry_observed_at ?? decided_at,
    entry_mid: snapshot.mid,
    entry_best_bid: snapshot.best_bid,
    entry_best_ask: snapshot.best_ask,
    entry_spread: input.entry_spread ?? null,
    assumed_size_usd: input.assumed_size_usd,
    slippage_rule: input.slippage_rule,
    assumed_entry_price: assumedEntryPrice(
      { ...snapshot, mid: snapshot.mid },
      input.slippage_rule,
      input.outcome,
    ),
    expiry_horizon_s: input.expiry_horizon_s,
    expires_at: new Date(
      parseIsoToEpochMs(decided_at) + input.expiry_horizon_s * 1000,
    ).toISOString(),
    markout_horizons_s: input.markout_horizons_s,
    invalidations: input.invalidations,
    evidence_refs: [],
    paper_only: true,
    fill_type: 'SIMULATED_NO_FILL',
    created_at: decided_at,
  };

  return store.ledger.insert(entry);
}
