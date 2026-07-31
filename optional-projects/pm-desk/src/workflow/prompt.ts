import { ADJUDICATION_VERSION, SLIPPAGE_RULES } from '../schema/adjudication.js';
import type { SignalEnvelope } from '../schema/signal.js';
import type { DeskStore } from '../store/index.js';

/**
 * Renders the adjudication prompt deterministically from a signal envelope plus
 * the evidence it references.
 *
 * The agent gets exactly this text and no tools. It cannot browse, search or
 * shell out, so every claim in its answer has to be traceable to something in
 * here — which is the property that makes the resulting alert auditable rather
 * than merely confident.
 */
export interface RenderPromptOptions {
  /** Max characters of evidence body to inline. Keeps the prompt bounded. */
  maxEvidenceChars?: number;
}

export function renderAdjudicationPrompt(
  store: DeskStore,
  signal: SignalEnvelope,
  options: RenderPromptOptions = {},
): string {
  const maxEvidence = options.maxEvidenceChars ?? 4000;
  const lines: string[] = [];

  lines.push('# Signal adjudication — PAPER ONLY research desk');
  lines.push('');
  lines.push(
    'A deterministic monitor detected the change below and captured the evidence. You are not being asked to find anything new — you have no tools, and no way to look anything up. Judge only what is in this document.',
  );
  lines.push('');
  lines.push('## Your decision');
  lines.push('');
  lines.push('Choose exactly one:');
  lines.push(
    '- `ignore`     — not material, already priced, duplicate, or the source does not bear on the market.',
  );
  lines.push(
    '- `watch`      — plausibly material but unconfirmed, mis-aligned, or the market is not tradable/live.',
  );
  lines.push(
    '- `paper_alert` — material, aligned with the resolution criterion, novel, and the market is still live.',
  );
  lines.push('');
  lines.push(
    'There is no fourth option. This desk cannot place, sign or route an order, and a `paper_alert` records a hypothetical entry in a local ledger and nothing else.',
  );
  lines.push('');

  lines.push('## Signal');
  lines.push('');
  lines.push(`- signal_id: ${signal.signal_id}`);
  lines.push(`- kind: ${signal.kind}`);
  lines.push(`- severity as assessed by the rule: ${signal.severity}`);
  lines.push(`- observed_at: ${signal.observed_at}`);
  lines.push(`- rule: ${signal.rule_id} v${signal.rule_version}`);
  lines.push(`- paper_only: ${signal.paper_only}`);
  lines.push('');

  if (signal.market_refs.length > 0) {
    lines.push('## Linked market(s)');
    lines.push('');
    for (const ref of signal.market_refs) {
      lines.push(`- market_id ${ref.market_id}`);
      if (ref.question) lines.push(`  - question: ${ref.question}`);
      if (ref.outcome) lines.push(`  - outcome under consideration: ${ref.outcome}`);
      if (ref.condition_id) lines.push(`  - condition_id: ${ref.condition_id}`);
      if (ref.token_id) lines.push(`  - token_id: ${ref.token_id}`);
      if (ref.end_date) lines.push(`  - resolves: ${ref.end_date}`);
    }
    lines.push('');
  } else {
    lines.push('## Linked market(s)');
    lines.push('');
    lines.push(
      'None. This signal is not bound to a market, which by itself is a strong reason not to raise a paper alert.',
    );
    lines.push('');
  }

  const snapshot = signal.market_snapshot;
  lines.push('## Market observation');
  lines.push('');
  if (snapshot) {
    lines.push(`- observed_at: ${snapshot.observed_at}`);
    lines.push(`- mid: ${fmt(snapshot.mid)}`);
    lines.push(`- best bid / best ask: ${fmt(snapshot.best_bid)} / ${fmt(snapshot.best_ask)}`);
    lines.push(`- spread: ${fmt(snapshot.spread)}`);
    if (snapshot.book_available === false) {
      lines.push(
        '- NOTE: the order book was unavailable at observation time. A price you cannot trade against is not evidence of consensus.',
      );
    }
  } else {
    lines.push(
      'No market observation accompanies this signal. You cannot assess whether the market has already repriced.',
    );
  }
  lines.push('');

  if (signal.source_refs.length > 0) {
    lines.push('## Primary source evidence');
    lines.push('');
    for (const ref of signal.source_refs) {
      lines.push(`### ${ref.source_id}`);
      lines.push(`- url: ${ref.url}`);
      lines.push(`- collected_at: ${ref.collected_at ?? signal.observed_at}`);
      lines.push(`- fingerprint: ${ref.previous_hash ?? '(none)'} → ${ref.current_hash}`);
      lines.push(`- artifact_ref: ${ref.artifact_ref}`);
      const body = safeRead(store, ref.artifact_ref);
      if (body) {
        lines.push('');
        lines.push('Extracted content as collected:');
        lines.push('```');
        lines.push(truncate(body, maxEvidence));
        lines.push('```');
      }
      lines.push('');
    }
  }

  lines.push('## What changed');
  lines.push('');
  if (signal.evidence.diff_excerpt) {
    lines.push('```diff');
    lines.push(truncate(signal.evidence.diff_excerpt, maxEvidence));
    lines.push('```');
    lines.push('');
  }
  if (signal.evidence.claims.length > 0) {
    lines.push('Rule-computed facts:');
    for (const claim of signal.evidence.claims) lines.push(`- ${claim}`);
    lines.push('');
  }
  if (signal.evidence.metrics) {
    lines.push('Rule-computed metrics:');
    for (const [key, value] of Object.entries(signal.evidence.metrics)) {
      lines.push(`- ${key}: ${value}`);
    }
    lines.push('');
  }

  lines.push('## What to assess');
  lines.push('');
  lines.push(
    '1. **Semantic alignment** — does the changed fact bear on the *exact* criterion the market resolves on? A source about a related-but-different statistic, period or entity is a `watch` at best. Say so explicitly if the mapping is loose.',
  );
  lines.push(
    '2. **Resolution mapping** — which specific published value decides this market, and is that the value that changed?',
  );
  lines.push(
    '3. **Novelty** — could this already be priced in? Compare the observation timestamp against the source timestamp. A market that already moved is not an opportunity.',
  );
  lines.push(
    '4. **Still live** — is the market active and does it resolve after this information matters?',
  );
  lines.push(
    '5. **Invalidation** — state the concrete fact that would prove this call wrong. If you cannot name one, that is itself a reason to choose `watch`.',
  );
  lines.push('');
  lines.push(
    'Be willing to answer `ignore`. Most detected changes are not actionable, and a desk that raises alerts on all of them is useless.',
  );
  lines.push('');

  lines.push('## Output');
  lines.push('');
  lines.push('Return a single JSON object matching this shape, and nothing else:');
  lines.push('');
  lines.push('```json');
  lines.push(
    JSON.stringify(
      {
        version: ADJUDICATION_VERSION,
        signal_id: signal.signal_id,
        decision: 'ignore | watch | paper_alert',
        rationale: 'Why, referencing the evidence above.',
        alignment: {
          market_source_aligned: true,
          notes: 'How the source maps to the resolution criterion.',
          resolution_mapping: 'The specific published value that decides this market.',
        },
        novelty: 'novel | already_priced | duplicate | unknown',
        still_live: true,
        invalidation: 'The concrete fact that would prove this wrong.',
        telegram_message: 'Concise operator message. Must begin with "PAPER ONLY".',
        ledger_proposal: {
          thesis: 'Required only for paper_alert; omit otherwise.',
          candidate_outcome: 'YES | NO',
          assumed_size_usd: 100,
          slippage_rule: SLIPPAGE_RULES.join(' | '),
          expiry_horizon_s: 86400,
          markout_horizons_s: [300, 3600],
          invalidations: ['At least one.'],
        },
        paper_only: true,
      },
      null,
      2,
    ),
  );
  lines.push('```');
  lines.push('');
  lines.push(
    '`paper_only` must be `true`. Omit `ledger_proposal` entirely unless the decision is `paper_alert`.',
  );

  return lines.join('\n');
}

function safeRead(store: DeskStore, ref: string): string | null {
  try {
    return store.artifacts.read(ref);
  } catch {
    // A missing artifact is itself information: the adjudicator should see the
    // ref without the body rather than have the whole render fail.
    return null;
  }
}

function fmt(value: number | null | undefined): string {
  return value === null || value === undefined ? 'unavailable' : String(value);
}

function truncate(value: string, maxChars: number): string {
  return value.length <= maxChars ? value : `${value.slice(0, maxChars)}\n… (truncated)`;
}
