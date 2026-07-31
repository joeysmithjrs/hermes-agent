# PM Desk — Hermes-Fork Integration and Carry-Forward Directive

## Mission

You are working in Joe's personal fork of Hermes Agent, not a standalone product repository:

- Worktree: `/home/hermes/research/hermes-pm-desk-mvp`
- Branch: `feat/pm-desk-mvp`
- Base: current local `main` / `origin/main`
- Existing PR: https://github.com/joeysmithjrs/hermes-agent/pull/9
- The branch has one transplant commit, `0ee45ceb7 feat(pm-desk): add paper-only prediction market desk MVP`, which placed a substantial TypeScript project under top-level `pm-desk/`.
- Original standalone source repo remains at `/home/hermes/pm-desk`, with later standalone commit `266131d fix(pm-demo): terminate direct ingress process reliably` that **was not included** in the transplant. It fixes leaked `tsx` child process behavior in `scripts/demo-offline.sh`.

Your job is to make this a correct, maintainable Hermes-fork integration and carry every intended PM-desk change forward correctly. Do not merely say it is correct: inspect the real code, actual Hermes runtime interfaces, installed CLI behavior, AGENTS.md architectural rules, CI config, and Git history; modify whatever needs modification; test it; make small coherent commits; then push updates to the existing PR branch. Do **not** merge PR #9 or create another PR.

## Existing state and verified facts

1. Existing branch head: `0ee45ceb7`; existing branch has exactly one giant transplant commit.
2. The source standalone history has the missing demo cleanup commit. Port its **substance** (not necessarily original commit topology) and ensure the exact demo cleanup works in the fork checkout.
3. The original standalone checkout passed its test suite with normal `npm ci`: 198 tests, lint, typecheck, build, guard test. Its demo was independently run after the cleanup patch and left no listener on port 8788.
4. A verification command using `npm ci --ignore-scripts` failed because it suppresses `better-sqlite3` native-binding install. This is expected, but it exposes a portability/verification contract that needs clear docs and potentially CI coverage.
5. Current `npm ci` in this fork worktree emits engine warnings: `@polymarket/client@0.2.0` / bindings/types declare `node >=24`, while this host is Node 22. A normal install currently succeeded and the test suite passed in the original checkout. Do not wave this away; assess whether the dependency/version/install strategy is actually appropriate for this Hermes fork.
6. `npm audit` in this fork worktree reported 5 high-severity transitive advisories after installing PM Desk dependencies. The original standalone directory earlier reported 0 with an omit-dev invocation. Resolve this discrepancy by determining the exact advisory path and write an honest conclusion. Do not conceal advisories.
7. CI on PR #9 passed the fork’s Python/OSV/etc. checks but **skipped JS & TS checks**, because the added top-level `pm-desk/` is outside the fork’s configured JS workspaces/affected-area selection. `Review label gate` failed because the PR lacks the expected review label; do not treat that label gate as a product failure, but report it.
8. Hermes workflow runtime facts:
   - `hermes workflow validate`, `run`, and `run-catalog` are present.
   - `hermes workflow run <path> --input '<json>'` is the actual CLI shape. There is no `--input-file` flag.
   - Native workflow `run:` uses a frozen script allowlist. Do not add a fake `pm_desk.*` callable that will not work.
   - Agent `spec.tools: []` is the enforced no-tool grant. `deny_tools` is not an enforced security boundary.
   - Global webhook config is currently disabled. Do not modify the live profile config, enable listeners, or add secrets.
9. PM Desk safety boundary is absolute in this task: no wallet, signing, `createSecureClient`, trade/order/UMA capability, account login/browser session persistence, or external live-trading surface. Browserbase remains deterministic primary-source collection only, explicitly opt-in.

## Read before coding

Read the root `AGENTS.md` in full enough to apply its project rules. In particular, Hermes' narrow-waist rule says third-party/vendor SaaS integrations should not become core model tools. Assess placement accordingly. This project must be usable from the Hermes fork but must not contaminate the root package/tool schema with desk-specific Node dependencies or create a fake core integration.

Read real interfaces and implementation for:

- `workflow/`, `workflow/runtime/live.py`, `workflow/runtime/scripts.py`, `workflow/verify.py`, `workflow/prompts/library.py`;
- `hermes_cli/subcommands/workflow*` / `workflow/cli.py` and actual `hermes workflow --help`;
- `hermes_cli/subcommands/webhook.py`, webhook docs/source, native cron runner if relevant;
- root `package.json`, package lock layout, JS CI selection in `.github/workflows/`;
- `tools/workflow_tools.py` if claiming an in-agent tool path;
- the PM Desk TypeScript source/tests/README/BUILD_SUMMARY currently on the branch;
- the original standalone commit and source diff for `266131d`.

## Required work

### A. Correct repository placement and package integration

Determine, from real Hermes repo conventions and AGENTS rules, the smallest correct in-fork home for the PM desk.

- Do **not** keep it as a random unintegrated top-level app if a project convention gives it a better home.
- Do **not** add its Node dependencies to root `package.json` or change the model tool schema.
- If the best solution is an optional/edge-owned package under a dedicated, discoverable directory, implement that placement and document why.
- It must remain versioned inside this fork and runnable independently with an explicit package-level install/test command.
- Make root-level discoverability appropriate: add a short, factual reference in a fitting existing doc/index if and only if it is warranted by repository conventions; do not rewrite the main Hermes README into a PM product pitch.
- Ensure Git ignore patterns prevent PM Desk runtime DBs, artifacts, node_modules, secrets, Browserbase recordings, and build output from being tracked from its final location.

### B. Carry all intended commits/substance forward

- Port the `266131d` demo cleanup behavior exactly or supersede it with an objectively better solution.
- Compare the source standalone tree (`/home/hermes/pm-desk` at its `feat/pm-desk-mvp` head) to the in-fork desk package after relocation/integration. Produce a machine-verifiable inventory or test proving intended tracked source files are preserved, excluding intentional placement/path changes and untracked runtime directories.
- Preserve authorship/credit where practical. The initial transplant is already one commit; do not churn/rewrite shared remote history unless absolutely necessary. Add corrective commits on top rather than force-pushing rewritten history.
- Do not copy `node_modules`, any `.env`, local DBs, artifacts, output logs, or cached build results.

### C. Real Hermes wiring audit and repair

The phrase “Hermes integration” must mean real, testable wiring—not prose:

1. **Workflow**
   - The paper research and signal-adjudication YAML must validate against the real installed/local Hermes workflow implementation.
   - The adjudication workflow must receive a valid strict SignalEnvelope input and render its prompt via the real workflow engine's supported prompt mechanism.
   - It must remain tools-empty; demonstrate this with a structural test/inspection against compiled IR or real validation.
   - Do not claim a workflow can write PM Desk’s SQLite ledger if it cannot. Architect the boundary honestly: deterministic PM Desk code owns record/write; the workflow produces a validated adjudication artifact; an explicit local handler translates a `paper_alert` into a paper ledger entry. Implement/test that actual bridge if it is necessary and safe. No LLM is needed for monitor detection.
   - If workflows need prompt-library files installed beneath `$HERMES_HOME/workflows/prompts`, write an explicit **opt-in installer/registration command** with a dry-run and a temp-HERMES_HOME integration test. Do not write to the actual `/home/hermes/.hermes` during tests or installation by default.

2. **Signal ingress / wake-up**
   - Retain loopback-only HMAC ingress with record-before-dispatch and outbox default.
   - The opt-in launcher must call the real workflow CLI with the exact accepted args and must have an actual integration test using a temporary `HERMES_HOME` / fake controlled runner or real workflow dry-run. It must never change global webhook config.
   - Clearly separate (a) PM Desk local ingress, (b) optional Hermes native webhook configuration, and (c) optional cron scheduling. No agent polling loop.

3. **Workspace/project operational surface**
   - Add an explicit safe setup command/documented sequence for binding the PM Desk directory to a Hermes project/workspace if that is the supported primitive. Test or dry-run it against a temp profile/home if reasonably possible.
   - Do not add a fake `workspace:` per-node guarantee: current workflow runtime does not enforce node-level workspace/profile isolation.

4. **Scheduling**
   - If you provide an example scheduled monitor invocation, make it a script-first, no-agent, observe-and-report-only cron recipe and document it as opt-in. Do not create a real cron job in the user profile.

### D. Node/dependency and CI correctness

- Analyze the `@polymarket/client` Node >=24 engine requirement. Choose one:
  1. declare Node 24 as a hard prerequisite for the PM Desk package and give a clear failing preflight; or
  2. pin a demonstrated compatible official SDK version that supports the fork/host Node policy; or
  3. otherwise solve it properly.

  Do not claim Node 22 support if the official package declares it unsupported just because a smoke passed.

- Analyze all production dependency vulnerabilities reported by `npm audit` from this package, including the difference between `--omit=dev` and normal audit. Avoid blind `npm audit fix --force`; update/pin only after understanding compatibility. If no safe fix exists, leave an explicit non-passable documented limitation and ensure the desk is not presented as ready for deployment.
- Add a package-level CI workflow/check that actually executes PM Desk install + test + lint + typecheck + build on the supported Node version. It must run when PM Desk files change. It must not force Node dependencies on normal Hermes users or root postinstall.
- The check must be appropriately named and become visible on PRs. Keep it scoped to this package and consistent with existing CI patterns.

### E. Tests and verification

Use test-first when correcting bugs. Run and report actual results for:

- PM Desk clean install on documented/supported Node version;
- PM Desk full offline tests, lint, typecheck, build, guard test;
- offline E2E demo and assert it does not leave a listener/child process;
- real Hermes workflow validation and dry run for both YAMLs;
- temp-HERMES_HOME prompt install/registration and workflow rendering/validation, if you implement installer wiring;
- controlled local ingress -> outbox -> exact workflow-launcher command behavior;
- the new PM Desk CI workflow lint/schema if the repo has a way to validate it locally;
- git status, diff, check-ignore for runtime/secret paths; and
- GitHub PR checks after push.

Do not run Browserbase live collection, enable global webhooks, create cron jobs, or call a live LLM workflow unless a safe no-cost test is already present and it is necessary. Do not trade.

## CI / PR operation

- Make small, conventional commits on the existing branch. The branch is already published; push your commits to `origin/feat/pm-desk-mvp` when verified.
- Update the existing PR #9 body if its summary/verification/package location become inaccurate. Do not create a new PR, merge, or force push.
- If the review-label gate is still the only failing check, identify the required label from repository workflow/config and apply the legitimate appropriate label only if Joe's existing role/permissions permit it and it accurately represents the PR. Do not apply a label merely to silence a gate if it misrepresents review status.
- Inspect CI after push. Fix real product/CI failures, not label-policy/process failures without legitimate authority.

## Definition of done

1. PM Desk is correctly and cleanly located inside the Hermes fork, remains edge-owned/self-contained, and has no root-core/tool-schema dependency pollution.
2. Missing standalone fix and all intended source are carried forward, verified by an inventory/comparison.
3. Real workflow, local ingress, prompt installation/registration (if needed), workspace/project setup, and optional script-first scheduling paths are correctly wired or explicitly limited—no fictional integrations.
4. Safety boundary is still enforced and tests prove it.
5. Node support is honestly resolved and documented; security/audit result is honest.
6. PM Desk receives actual package-scoped PR CI on its declared supported Node version.
7. All relevant tests/checks pass; no runtime/secrets are tracked.
8. Commits are pushed to PR #9; `INTEGRATION_SUMMARY.md` records exact paths, commands, outcomes, intentional limits, and the source-tree carry-forward proof.

When done, do not claim success based only on a summary. Leave the working tree clean and give precise commands/results in `INTEGRATION_SUMMARY.md`.
