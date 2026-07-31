import { deterministicId } from '../core/ids.js';
import { secondsBetween, systemClock } from '../core/time.js';
import type { MonitorSpec } from '../schema/monitor-spec.js';
import {
  parseSignalEnvelope,
  SIGNAL_ENVELOPE_VERSION,
  type SignalEnvelope,
} from '../schema/signal.js';
import type { DeskStore } from '../store/index.js';
import { evaluateRule } from './rules.js';
import type { EvaluateOptions, MonitorEvaluation, RuleCandidate } from './types.js';

/**
 * The detector path. No agent, no prompt, no network — stored evidence in,
 * `SILENT` or one schema-valid `SignalEnvelope` out.
 *
 * Order of suppression is deliberate:
 *   disabled → predicate → duplicate → cooldown → emit
 * A duplicate is reported as a duplicate even when the cooldown has also
 * elapsed, because that is the more informative fact for an operator reading
 * the audit trail.
 */
export function evaluateMonitor(
  store: DeskStore,
  spec: MonitorSpec,
  options: EvaluateOptions = {},
): MonitorEvaluation {
  const clock = options.clock ?? systemClock;
  const dryRun = options.dryRun ?? false;
  const now = clock.now();
  const ruleVersion = String(spec.version);

  const base = {
    rule_id: spec.id,
    rule_version: ruleVersion,
    evaluated_at: now,
    dryRun,
  };

  const finish = (
    outcome: MonitorEvaluation['outcome'],
    reason: string,
    extra: { signal?: SignalEnvelope; dedupe_key?: string } = {},
  ): MonitorEvaluation => {
    if (!dryRun) {
      store.monitorState.recordDecision({
        rule_id: spec.id,
        rule_version: ruleVersion,
        evaluated_at: now,
        outcome,
        reason,
        dedupe_key: extra.dedupe_key,
        signal_id: extra.signal?.signal_id,
      });
    }
    return { ...base, outcome, reason, ...(extra.signal ? { signal: extra.signal } : {}) };
  };

  if (!spec.enabled) {
    // A disabled rule writes nothing at all, not even an audit row.
    return { ...base, outcome: 'skipped_disabled', reason: 'rule is disabled in its spec' };
  }

  const result = evaluateRule(store, spec, now);
  if (!result.fired) {
    return finish(result.outcome, result.reason);
  }

  const candidate = result.candidate;
  const signal = buildEnvelope(spec, candidate, ruleVersion);

  const existing = store.signals.getByDedupeKey(candidate.dedupe_key);
  if (existing) {
    return finish(
      'suppressed_duplicate',
      `dedupe_key already recorded as ${existing.envelope.signal_id}`,
      { dedupe_key: candidate.dedupe_key },
    );
  }

  const lastEmission = store.monitorState.lastEmissionForRule(spec.id);
  if (lastEmission && spec.cooldown_s > 0) {
    const elapsed = secondsBetween(lastEmission.last_emitted_at, now);
    if (elapsed < spec.cooldown_s) {
      return finish(
        'suppressed_cooldown',
        `${Math.round(elapsed)}s since last emission, cooldown_s=${spec.cooldown_s}`,
        { dedupe_key: candidate.dedupe_key },
      );
    }
  }

  if (dryRun) {
    return { ...base, outcome: 'emitted', signal };
  }

  store.transaction(() => {
    store.signals.record(signal, 'monitor');
    store.monitorState.markEmitted(spec.id, candidate.dedupe_key, now, signal.signal_id, {
      observed_at: candidate.observed_at,
    });
  });

  return finish('emitted', 'predicate fired', { signal, dedupe_key: candidate.dedupe_key });
}

function buildEnvelope(
  spec: MonitorSpec,
  candidate: RuleCandidate,
  ruleVersion: string,
): SignalEnvelope {
  const envelope = {
    version: SIGNAL_ENVELOPE_VERSION,
    // Derived from the dedupe key, so the same facts always yield the same id.
    signal_id: deterministicId('sig', [spec.id, ruleVersion, candidate.dedupe_key]),
    kind: candidate.kind,
    severity: spec.severity,
    observed_at: candidate.observed_at,
    rule_id: spec.id,
    rule_version: ruleVersion,
    market_refs: candidate.market_refs,
    source_refs: candidate.source_refs,
    market_snapshot: candidate.market_snapshot,
    evidence: candidate.evidence,
    paper_only: true as const,
    dedupe_key: candidate.dedupe_key,
  };
  // Validate before the envelope can reach the store, the ingress or a prompt.
  return parseSignalEnvelope(envelope);
}
