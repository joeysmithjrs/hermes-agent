import { describe, expect, it } from 'vitest';

import {
  computePlanId,
  executionPlanJsonSchema,
  parseExecutionPlan,
  type ExecutionPlan,
} from '../src/schema/execution-plan.js';

/**
 * The ExecutionPlan is the only thing standing between an agent's opinion and
 * the provisioner installing something, so these tests are about what the
 * schema REFUSES, not what it accepts.
 */

/** A minimal plan that passes, used as the base every negative case mutates. */
function validPlan(): Record<string, unknown> {
  return {
    version: 1,
    plan_id: 'plan_test',
    paper_only: true,
    created_at: '2026-07-31T06:00:00.000Z',
    seed: {
      seed_id: 'seed_abc',
      run_date: '2026-07-31',
      taxonomy_version: 1,
      desk_state_hash: 'a'.repeat(64),
    },
    thesis: {
      title: 'Statistical release revision resolves a market before its stated horizon',
      edge_mechanism:
        'The primary release page publishes the revision on a fixed schedule; the desk fingerprints it and sees the change before the market reprices.',
      market_refs: [{ market_id: 'mkt_1', question: 'Will Q1 GDP be revised below 2.5%?' }],
      horizon: '6 days',
      invalidations: ['the agency delays the release', 'the market rules cite a different series'],
      why_not_retail_hopium:
        'It rests on a scheduled primary-source publication, not on a narrative about what should happen.',
    },
    research_summary: {
      source_graph: [
        {
          source: 'Bureau of Economic Analysis release page',
          authority: 'primary',
          claim: 'Second estimate is published on the scheduled date.',
          url: 'https://www.bea.gov/data/gdp',
        },
      ],
      rules_resolution: 'Resolves to the headline figure in the second estimate.',
    },
    monitors: [
      {
        monitor_id: 'gdp_release_change',
        kind: 'primary_source_change',
        priority: 'script_first',
        spec_inline: {
          id: 'gdp_release_change',
          kind: 'primary_source_change',
          params: { source_id: 'bea_gdp_release' },
        },
        schedule: '30m',
        on_fire: ['notify_telegram', 'queue_ingress'],
      },
    ],
    hermes_setup: [
      {
        action: 'cron_create',
        dry_run_command: 'hermes cron create 30m --no-agent --script pm-desk-plan_test-30m.sh',
        apply_command: 'hermes cron create 30m --no-agent --script pm-desk-plan_test-30m.sh',
        idempotency_key: 'plan_test:cron:30m',
      },
    ],
    telegram_brief: 'PAPER ONLY. Watch the BEA release page for the second estimate.',
    approval: { required: true, dual_control: true, channel: 'telegram', decision: 'pending' },
    paper_only_constraints: {
      live_execution_allowed: false,
      fill_type_if_ledger: 'SIMULATED_NO_FILL',
    },
  };
}

describe('ExecutionPlan schema', () => {
  it('accepts a well-formed plan and applies array defaults', () => {
    const plan = parseExecutionPlan(validPlan());
    expect(plan.plan_id).toBe('plan_test');
    expect(plan.research_summary.unknowns).toEqual([]);
    expect(plan.research_summary.dq_notes).toEqual([]);
    expect(plan.monitors[0]?.spec_inline).toBeDefined();
  });

  it('cannot represent a live-execution plan', () => {
    expect(() => parseExecutionPlan({ ...validPlan(), paper_only: false })).toThrow(/paper_only/);
    expect(() =>
      parseExecutionPlan({
        ...validPlan(),
        paper_only_constraints: {
          live_execution_allowed: true,
          fill_type_if_ledger: 'SIMULATED_NO_FILL',
        },
      }),
    ).toThrow(/live_execution_allowed/);
  });

  it('pins the ledger fill type so an approved plan cannot imply a real fill', () => {
    expect(() =>
      parseExecutionPlan({
        ...validPlan(),
        paper_only_constraints: { live_execution_allowed: false, fill_type_if_ledger: 'FILLED' },
      }),
    ).toThrow(/fill_type_if_ledger/);
  });

  it('rejects an unknown top-level field rather than dropping it silently', () => {
    expect(() => parseExecutionPlan({ ...validPlan(), order_size_usd: 500 })).toThrow(
      /order_size_usd|Unrecognized/,
    );
  });

  it('requires a reason when a plan installs no monitors', () => {
    const base = validPlan();
    expect(() => parseExecutionPlan({ ...base, monitors: [], hermes_setup: [] })).toThrow(
      /no_monitors_reason/,
    );
    const withReason = parseExecutionPlan({
      ...base,
      monitors: [],
      hermes_setup: [],
      no_monitors_reason: 'The thesis is real but nothing observable changes before resolution.',
    });
    expect(withReason.monitors).toEqual([]);
  });

  it('requires a declarative monitor to carry a MonitorSpec body', () => {
    const base = validPlan();
    const monitors = [{ ...(base['monitors'] as Record<string, unknown>[])[0] }];
    delete monitors[0]!['spec_inline'];
    expect(() => parseExecutionPlan({ ...base, monitors })).toThrow(/spec_inline/);
  });

  it('requires a custom_script monitor to carry a script block', () => {
    const base = validPlan();
    const monitors = [
      {
        monitor_id: 'bespoke',
        kind: 'custom_script',
        priority: 'script_first',
        schedule: '1h',
        on_fire: ['notify_telegram'],
      },
    ];
    expect(() => parseExecutionPlan({ ...base, monitors })).toThrow(/script/);

    const ok = parseExecutionPlan({
      ...base,
      monitors: [
        {
          ...monitors[0],
          script: { path_hint: 'bespoke.sh', args: ['--home', 'data'], silent_when_empty: true },
        },
      ],
    });
    expect(ok.monitors[0]?.script?.silent_when_empty).toBe(true);
  });

  it('refuses a script that admits it is not silent when nothing fires', () => {
    const base = validPlan();
    expect(() =>
      parseExecutionPlan({
        ...base,
        monitors: [
          {
            monitor_id: 'chatty',
            kind: 'custom_script',
            priority: 'script_first',
            schedule: '1h',
            on_fire: ['notify_telegram'],
            script: { path_hint: 'chatty.sh', args: [], silent_when_empty: false },
          },
        ],
      }),
    ).toThrow(/silent_when_empty/);
  });

  it('rejects duplicate monitor ids and duplicate idempotency keys', () => {
    const base = validPlan();
    const monitor = (base['monitors'] as Record<string, unknown>[])[0]!;
    expect(() => parseExecutionPlan({ ...base, monitors: [monitor, { ...monitor }] })).toThrow(
      /unique/,
    );

    const setup = (base['hermes_setup'] as Record<string, unknown>[])[0]!;
    expect(() => parseExecutionPlan({ ...base, hermes_setup: [setup, { ...setup }] })).toThrow(
      /unique/,
    );
  });

  it('requires a decided approval to say when it was decided', () => {
    const base = validPlan();
    expect(() =>
      parseExecutionPlan({
        ...base,
        approval: { required: true, dual_control: true, channel: 'telegram', decision: 'approved' },
      }),
    ).toThrow(/decided_at/);
  });

  it('cannot express a single-control or non-telegram approval', () => {
    const base = validPlan();
    for (const approval of [
      { required: true, dual_control: false, channel: 'telegram', decision: 'pending' },
      { required: false, dual_control: true, channel: 'telegram', decision: 'pending' },
      { required: true, dual_control: true, channel: 'cli', decision: 'pending' },
    ]) {
      expect(() => parseExecutionPlan({ ...base, approval })).toThrow();
    }
  });

  it('requires at least one primary or secondary source and one market ref', () => {
    const base = validPlan();
    expect(() =>
      parseExecutionPlan({
        ...base,
        research_summary: {
          ...(base['research_summary'] as Record<string, unknown>),
          source_graph: [],
        },
      }),
    ).toThrow();
    expect(() =>
      parseExecutionPlan({
        ...base,
        thesis: { ...(base['thesis'] as Record<string, unknown>), market_refs: [] },
      }),
    ).toThrow();
  });

  it('rejects a market ref that identifies nothing', () => {
    const base = validPlan();
    expect(() =>
      parseExecutionPlan({
        ...base,
        thesis: { ...(base['thesis'] as Record<string, unknown>), market_refs: [{}] },
      }),
    ).toThrow();
  });

  it('requires at least one invalidation — a thesis that cannot be wrong is not one', () => {
    const base = validPlan();
    expect(() =>
      parseExecutionPlan({
        ...base,
        thesis: { ...(base['thesis'] as Record<string, unknown>), invalidations: [] },
      }),
    ).toThrow(/invalidations/);
  });

  it('keeps the telegram brief inside one Telegram message', () => {
    const base = validPlan();
    expect(() => parseExecutionPlan({ ...base, telegram_brief: 'x'.repeat(4097) })).toThrow();
  });
});

describe('computePlanId', () => {
  const input = {
    seed_id: 'seed_abc',
    run_date: '2026-07-31',
    thesis_title: 'A title',
    market_refs: [{ market_id: 'mkt_1' }],
  };

  it('is deterministic, so a retried morning run provisions the same plan', () => {
    expect(computePlanId(input)).toBe(computePlanId(input));
    // Key order in the market refs must not change the id.
    expect(
      computePlanId({ ...input, market_refs: [{ market_id: 'mkt_1', question: undefined }] }),
    ).toBe(computePlanId(input));
  });

  it('separates plans that differ in any component', () => {
    const ids = new Set([
      computePlanId(input),
      computePlanId({ ...input, run_date: '2026-08-01' }),
      computePlanId({ ...input, seed_id: 'seed_xyz' }),
      computePlanId({ ...input, thesis_title: 'Another title' }),
      computePlanId({ ...input, market_refs: [{ market_id: 'mkt_2' }] }),
    ]);
    expect(ids.size).toBe(5);
  });
});

describe('executionPlanJsonSchema', () => {
  it('describes the input form, so defaulted fields are not marked required', () => {
    const schema = executionPlanJsonSchema() as {
      required: string[];
      properties: Record<string, unknown>;
    };
    expect(schema.required).toContain('thesis');
    expect(schema.required).toContain('telegram_brief');
    expect(schema.required).not.toContain('monitors');
    expect(schema.properties['paper_only']).toMatchObject({ const: true });
  });

  it('round-trips: a plan generated against this schema still parses', () => {
    // The schema is documentation; parseExecutionPlan is the enforcement. This
    // asserts they at least agree on the example.
    expect(() => parseExecutionPlan(validPlan())).not.toThrow();
    const plan: ExecutionPlan = parseExecutionPlan(validPlan());
    expect(parseExecutionPlan(JSON.parse(JSON.stringify(plan)))).toEqual(plan);
  });
});

describe('proposed_buildouts', () => {
  it('accepts pending Joe-gated build proposals', () => {
    const plan = validPlan();
    plan.proposed_buildouts = [
      {
        id: 'fr_api_poller',
        kind: 'datafeed_or_api',
        title: 'Federal Register JSON poller',
        problem: 'Need L0 HTTP path without CDN race',
        proposed_interface: 'pm-desk source collect http adapter for FR documents',
        validation_plan: 'Fixture FR response then live dry-run',
        cost_risk_notes: 'Public API, low cost',
        spawn_recommendation: 'claude_code_pro_after_approval',
        approval_required: true,
        decision: 'pending',
      },
    ];
    expect(() => parseExecutionPlan(plan)).not.toThrow();
  });

  it('rejects buildouts that set approval_required false', () => {
    const plan = validPlan();
    plan.proposed_buildouts = [
      {
        id: 'bad',
        kind: 'other',
        title: 'x',
        problem: 'p',
        proposed_interface: 'i',
        validation_plan: 'v',
        cost_risk_notes: 'c',
        spawn_recommendation: 'none',
        approval_required: false,
        decision: 'pending',
      },
    ];
    expect(() => parseExecutionPlan(plan)).toThrow();
  });

  it('rejects a buildout whose id names an already-shipped tool', () => {
    const plan = validPlan();
    plan.proposed_buildouts = [
      {
        id: 'cpi_nowcast_bucket_harness',
        kind: 'research_harness',
        title: 'Build the CPI nowcast harness',
        problem: 'need bucket calibration',
        proposed_interface: 'pm-desk research cpi-calibrate',
        validation_plan: 'fixture then live',
        cost_risk_notes: 'public data only',
        spawn_recommendation: 'none',
        approval_required: true,
        decision: 'pending',
      },
    ];
    expect(() => parseExecutionPlan(plan)).toThrow(/already shipped/);
  });
});
