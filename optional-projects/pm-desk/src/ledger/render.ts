import type { Adjudication } from '../schema/adjudication.js';
import type { SignalEnvelope } from '../schema/signal.js';
import type { PaperLedgerEntry } from './types.js';

/** Telegram's hard limit for a single text message. */
const MAX_MESSAGE_CHARS = 4096;

export interface RenderInput {
  signal: SignalEnvelope;
  adjudication: Adjudication;
  entry?: PaperLedgerEntry;
}

/**
 * Renders the operator-facing alert.
 *
 * Plain text, no markup, no links beyond the source URL itself. The first line
 * says PAPER ONLY because that is the one thing a reader must not miss while
 * skimming a phone notification, and the message never phrases anything as an
 * instruction to act — this desk has no way to act, and the message should not
 * imply otherwise.
 */
export function renderTelegramAlert(input: RenderInput): string {
  const { signal, adjudication, entry } = input;
  const lines: string[] = [];

  const decisionLabel = adjudication.decision.toUpperCase().replace('_', ' ');
  lines.push(`🧾 PAPER ONLY — ${decisionLabel} — ${signal.severity.toUpperCase()}`);
  lines.push('No order exists. Nothing was sent to any venue.');
  lines.push('');

  const market = signal.market_refs[0];
  if (market) {
    lines.push(`MARKET  ${truncate(market.question ?? market.market_id, 180)}`);
    const bits = [
      market.outcome ? `outcome ${market.outcome}` : null,
      `market_id ${market.market_id}`,
      market.end_date ? `resolves ${market.end_date}` : null,
    ].filter(Boolean);
    lines.push(`        ${bits.join(' · ')}`);
  } else {
    lines.push('MARKET  (no market linked to this signal)');
  }
  lines.push('');

  const source = signal.source_refs[0];
  if (source) {
    lines.push(`SOURCE  ${source.source_id}`);
    lines.push(`        ${source.url}`);
    lines.push(`        collected ${source.collected_at ?? signal.observed_at}`);
    if (source.previous_hash) {
      lines.push(
        `        fingerprint ${source.previous_hash.slice(0, 12)} → ${source.current_hash.slice(0, 12)}`,
      );
    }
    const claim = signal.evidence.claims[0];
    if (claim) lines.push(`        ${truncate(claim, 220)}`);
  } else {
    lines.push('SOURCE  (market-derived signal; no primary source attached)');
  }
  lines.push('');

  const snapshot = signal.market_snapshot;
  if (snapshot) {
    lines.push(`OBSERVED ${snapshot.observed_at}`);
    lines.push(
      `        mid ${fmt(snapshot.mid)} · bid ${fmt(snapshot.best_bid)} · ask ${fmt(snapshot.best_ask)} · spread ${fmt(snapshot.spread)}`,
    );
    if (snapshot.book_available === false) {
      lines.push('        order book unavailable at observation time');
    }
  } else {
    lines.push('OBSERVED (no market observation was recorded for this signal)');
  }
  lines.push('');

  lines.push(`CALL    ${truncate(adjudication.rationale, 500)}`);
  lines.push(
    `        alignment: ${adjudication.alignment.market_source_aligned ? 'source matches the resolution criterion' : 'SOURCE DOES NOT CLEANLY MATCH THE RESOLUTION CRITERION'}`,
  );
  lines.push(
    `        novelty: ${adjudication.novelty} · still live: ${adjudication.still_live ? 'yes' : 'no'}`,
  );
  lines.push('');

  lines.push(`INVALIDATED IF  ${truncate(adjudication.invalidation, 300)}`);
  if (entry && entry.invalidations.length > 0) {
    for (const item of entry.invalidations.slice(0, 4)) {
      lines.push(`        · ${truncate(item, 160)}`);
    }
  }
  lines.push('');

  if (entry) {
    lines.push(`PAPER ENTRY  ${entry.entry_id}`);
    lines.push(
      `        assumed ${entry.outcome} @ ${fmt(entry.assumed_entry_price)} · size $${entry.assumed_size_usd} · ${entry.slippage_rule}`,
    );
    lines.push(`        decided ${entry.decided_at} · expires ${entry.expires_at}`);
    lines.push(`        markouts ${entry.markout_horizons_s.map((s) => `${s}s`).join(', ')}`);
    lines.push(`        fill_type ${entry.fill_type}`);
    lines.push('');
  }

  lines.push('EVIDENCE');
  lines.push(`        signal ${signal.signal_id} · rule ${signal.rule_id} v${signal.rule_version}`);
  for (const ref of signal.source_refs.slice(0, 3)) {
    lines.push(`        artifact ${ref.artifact_ref}`);
  }
  if (entry) {
    lines.push(`        pm-desk ledger show --entry ${entry.entry_id}`);
  }

  return clamp(lines.join('\n'), MAX_MESSAGE_CHARS);
}

function fmt(value: number | null | undefined): string {
  return value === null || value === undefined ? 'n/a' : value.toFixed(4).replace(/0+$/, '0');
}

function truncate(value: string, maxChars: number): string {
  const single = value.replace(/\s+/g, ' ').trim();
  return single.length <= maxChars ? single : `${single.slice(0, maxChars - 1)}…`;
}

function clamp(text: string, maxChars: number): string {
  if (text.length <= maxChars) return text;
  const suffix = '\n… (truncated; see the ledger entry for the full record)';
  return `${text.slice(0, maxChars - suffix.length)}${suffix}`;
}
