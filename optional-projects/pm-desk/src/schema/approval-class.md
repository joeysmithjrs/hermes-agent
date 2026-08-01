approval_class: z.enum(['auto_agent', 'joe_infra', 'joe_live_risk']).default('joe_infra')
- auto_agent: public data/code (agent runs, no Joe gate)
- joe_infra: paid feeds/secrets/cloud cost
- joe_live_risk: live execution / non-paper
Built per control-plane plan.
