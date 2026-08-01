/**
 * The edge-first telegram brief contract.
 *
 * A brief that says "watch this, it looks interesting" is not actionable: Joe
 * has to extract the EV himself, guess why the gap exists, and infer what would
 * make him wrong. The contract below forces the five sections a thirty-second
 * decision actually needs, so a brief without them is flagged before it reaches
 * a gate rather than rubber-stamped.
 *
 * This is a *soft* check by default (warn) and a *hard* check under
 * `pm-desk plan validate --strict-brief` (the reopen workflow's posture). The
 * morning starts in warn mode; a reopen, which re-decides a real thesis with
 * real numbers, must pass strict.
 */

import type { ExecutionPlan } from '../schema/execution-plan.js';

/**
 * The five labeled sections an edge-first brief must contain, in spirit if not
 * in exact order. Matched case-insensitively as substrings of the brief.
 */
export const EDGE_FIRST_BRIEF_SECTIONS = [
  'CLAIM',
  'WHY GAP CAN EXIST',
  'MEASURED',
  'KILLS',
  'IF YOU APPROVE',
] as const;

export interface BriefCheck {
  /** True when every required section is present. */
  ok: boolean;
  /** The section labels that are missing, in declared order. */
  missing: readonly string[];
}

/**
 * Check a brief for the edge-first sections. Case-insensitive substring match,
 * so "Why the gap can exist:" or "why gap can exist —" both satisfy the label.
 */
export function checkEdgeFirstBrief(brief: string): BriefCheck {
  const upper = brief.toUpperCase();
  const missing = EDGE_FIRST_BRIEF_SECTIONS.filter((section) => !upper.includes(section));
  return { ok: missing.length === 0, missing };
}

/** Check a plan's telegram_brief. Convenience over {@link checkEdgeFirstBrief}. */
export function checkPlanBrief(plan: ExecutionPlan): BriefCheck {
  return checkEdgeFirstBrief(plan.telegram_brief);
}

/** A one-line human warning, or null when the brief is complete. */
export function briefWarning(plan: ExecutionPlan): string | null {
  const { ok, missing } = checkPlanBrief(plan);
  if (ok) return null;
  return `⚠ telegram_brief is missing edge-first section(s): ${missing.join(', ')}`;
}
