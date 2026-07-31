import type { ExecutionPlan } from '../schema/execution-plan.js';

/** Telegram's hard limit for a single text message. */
const MAX_MESSAGE_CHARS = 4096;

/**
 * The message Joe actually approves.
 *
 * The generator writes `telegram_brief` — its own edge story, in its own words.
 * This function wraps that story in the parts a human must not have to take on
 * trust: the paper-only banner, every monitor that will be installed, and every
 * `hermes_setup` command verbatim.
 *
 * That last part is the whole point. Dual control over a summary is theatre;
 * dual control means the approver saw the argv. So the commands are printed
 * unabridged and the message is truncated from the *brief* end if it overflows,
 * never from the command list.
 */
export function renderPlanTelegram(plan: ExecutionPlan): string {
  const head: string[] = [];

  head.push(`🧭 PAPER ONLY — EXECUTION PLAN — ${plan.approval.decision.toUpperCase()}`);
  head.push('Approving installs monitors. It places no order and touches no wallet.');
  head.push('');
  head.push(`PLAN    ${plan.plan_id}`);
  head.push(
    `        seed ${plan.seed.seed_id} · ${plan.seed.run_date} · taxonomy v${plan.seed.taxonomy_version}`,
  );
  head.push('');
  head.push(`THESIS  ${truncate(plan.thesis.title, 200)}`);
  head.push(`        horizon ${truncate(plan.thesis.horizon, 120)}`);
  for (const ref of plan.thesis.market_refs.slice(0, 3)) {
    const bits = [
      ref.question ? truncate(ref.question, 140) : undefined,
      ref.outcome ? `outcome ${ref.outcome}` : undefined,
      ref.market_id ? `market_id ${ref.market_id}` : undefined,
    ].filter(Boolean);
    head.push(`        · ${bits.join(' · ')}`);
  }
  head.push('');

  // Ordered by what a reader needs in order to say no. Overflow is dropped from
  // the end of this block, so the cheapest-to-lose material goes last.
  const brief: string[] = [];
  brief.push('BRIEF');
  for (const line of plan.telegram_brief.trim().split('\n')) {
    brief.push(line.trim() === '' ? '' : `        ${line}`);
  }
  brief.push('');
  brief.push('INVALIDATED IF');
  for (const item of plan.thesis.invalidations.slice(0, 5)) {
    brief.push(`        · ${truncate(item, 200)}`);
  }
  brief.push('');
  brief.push('SOURCES');
  for (const entry of plan.research_summary.source_graph.slice(0, 5)) {
    brief.push(
      `        [${entry.authority}] ${truncate(entry.source, 90)} — ${truncate(entry.claim, 160)}`,
    );
  }
  if (plan.research_summary.unknowns.length > 0) {
    brief.push('');
    brief.push('UNKNOWNS');
    for (const unknown of plan.research_summary.unknowns.slice(0, 5)) {
      brief.push(`        · ${truncate(unknown, 200)}`);
    }
  }
  brief.push('');
  brief.push(`EDGE    ${truncate(plan.thesis.edge_mechanism, 700)}`);
  brief.push(`NOT HOPIUM  ${truncate(plan.thesis.why_not_retail_hopium, 500)}`);
  brief.push('');

  const tail: string[] = [];
  tail.push(`MONITORS TO INSTALL (${plan.monitors.length})`);
  if (plan.monitors.length === 0) {
    tail.push(`        (none) ${truncate(plan.no_monitors_reason ?? 'no reason given', 300)}`);
  }
  for (const monitor of plan.monitors) {
    tail.push(`        ${monitor.monitor_id} — ${monitor.kind} · ${monitor.priority}`);
    tail.push(`          every ${monitor.schedule} → ${monitor.on_fire.join(', ')}`);
  }
  tail.push('');

  tail.push(`HERMES SETUP ON APPROVE (${plan.hermes_setup.length} command(s), verbatim)`);
  for (const item of plan.hermes_setup) {
    tail.push(`        [${item.action}]`);
    tail.push(`          ${item.apply_command}`);
  }
  if (plan.hermes_setup.some((s) => s.action === 'webhook_subscribe_recipe')) {
    tail.push('');
    tail.push('        Webhook items are printed as recipes and are never executed.');
    tail.push('        Enabling one is an edit to your Hermes config, which pm-desk never makes.');
  }
  if ((plan.proposed_buildouts?.length ?? 0) > 0) {
    tail.push('');
    tail.push(`PROPOSED BUILDOUTS (${plan.proposed_buildouts.length}) — NOT auto-installed; Joe must OK each`);
    for (const b of plan.proposed_buildouts) {
      tail.push(`        ${b.id} — ${b.kind} · spawn ${b.spawn_recommendation} · ${b.decision}`);
      tail.push(`          ${truncate(b.title, 160)}`);
    }
  }
  tail.push('');
  tail.push(`APPROVAL  dual control via ${plan.approval.channel} · live execution allowed: no`);
  tail.push(
    `          fill type if this ever reaches the ledger: ${plan.paper_only_constraints.fill_type_if_ledger}`,
  );

  return assemble(head, brief, tail);
}

/**
 * Join the three blocks, dropping brief lines (never the head or the command
 * list) until the whole message fits.
 */
function assemble(head: string[], brief: string[], tail: string[]): string {
  const fixed = head.length + tail.length;
  let body = brief;
  for (;;) {
    const text = [...head, ...body, ...tail].join('\n');
    if (text.length <= MAX_MESSAGE_CHARS || body.length === 0) {
      return text.length <= MAX_MESSAGE_CHARS ? text : hardClamp(text);
    }
    // Drop from the end of the brief, keeping its first lines and the marker.
    body = [...body.slice(0, Math.max(1, body.length - Math.ceil(fixed / 4) - 1)), '        …'];
    if (body.length <= 2) {
      const text2 = [...head, ...body, ...tail].join('\n');
      return text2.length <= MAX_MESSAGE_CHARS ? text2 : hardClamp(text2);
    }
  }
}

function hardClamp(text: string): string {
  const suffix = '\n… (truncated; run `pm-desk plan show --file <plan>` for the full plan)';
  return `${text.slice(0, MAX_MESSAGE_CHARS - suffix.length)}${suffix}`;
}

/** The terminal view: everything, no truncation, ordered for reading top-down. */
export function renderPlanSummary(plan: ExecutionPlan): string {
  const lines: string[] = [];
  lines.push(`PAPER ONLY — ExecutionPlan v${plan.version} — ${plan.plan_id}`);
  lines.push(
    `created ${plan.created_at}${plan.generator_run_id ? ` · run ${plan.generator_run_id}` : ''}`,
  );
  lines.push(
    `approval: ${plan.approval.decision}${plan.approval.decided_at ? ` at ${plan.approval.decided_at}` : ''}`,
  );
  lines.push('');
  lines.push(`THESIS  ${plan.thesis.title}`);
  lines.push(`  edge          ${plan.thesis.edge_mechanism}`);
  lines.push(`  horizon       ${plan.thesis.horizon}`);
  lines.push(`  not hopium    ${plan.thesis.why_not_retail_hopium}`);
  lines.push(`  markets       ${plan.thesis.market_refs.length}`);
  for (const ref of plan.thesis.market_refs) {
    lines.push(
      `    · ${[ref.question, ref.outcome, ref.market_id, ref.condition_id, ref.token_id].filter(Boolean).join(' · ')}`,
    );
  }
  lines.push('');
  lines.push('RESEARCH');
  lines.push(`  rules         ${plan.research_summary.rules_resolution}`);
  for (const entry of plan.research_summary.source_graph) {
    lines.push(`    [${entry.authority}] ${entry.source}${entry.url ? ` <${entry.url}>` : ''}`);
    lines.push(`        ${entry.claim}`);
  }
  for (const unknown of plan.research_summary.unknowns) lines.push(`  unknown       ${unknown}`);
  for (const note of plan.research_summary.dq_notes) lines.push(`  dq note       ${note}`);
  lines.push('');
  lines.push(`MONITORS (${plan.monitors.length})`);
  if (plan.monitors.length === 0)
    lines.push(`  none — ${plan.no_monitors_reason ?? '(no reason given)'}`);
  for (const monitor of plan.monitors) {
    lines.push(
      `  ${monitor.monitor_id}  ${monitor.kind}  ${monitor.priority}  every ${monitor.schedule}`,
    );
    lines.push(`    on fire: ${monitor.on_fire.join(', ')}`);
    if (monitor.script)
      lines.push(`    script:  ${monitor.script.path_hint} ${monitor.script.args.join(' ')}`);
    if (monitor.notes) lines.push(`    notes:   ${monitor.notes}`);
  }
  lines.push('');
  lines.push(`HERMES SETUP (${plan.hermes_setup.length})`);
  for (const item of plan.hermes_setup) {
    lines.push(`  ${item.action}  [${item.idempotency_key}]`);
    lines.push(`    dry-run: ${item.dry_run_command}`);
    lines.push(`    apply:   ${item.apply_command}`);
    if (item.notes) lines.push(`    notes:   ${item.notes}`);
  }
  lines.push('');
  lines.push(`live_execution_allowed: ${plan.paper_only_constraints.live_execution_allowed}`);
  return lines.join('\n');
}

function truncate(value: string, maxChars: number): string {
  const single = value.replace(/\s+/g, ' ').trim();
  return single.length <= maxChars ? single : `${single.slice(0, maxChars - 1)}…`;
}
