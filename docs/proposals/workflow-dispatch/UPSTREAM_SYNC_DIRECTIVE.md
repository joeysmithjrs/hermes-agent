# DIRECTIVE — Sync fork with upstream NousResearch/hermes-agent main

**Implementer:** you (Claude Code, Sonnet, running on Joe's Claude Pro subscription).
No multi-agent fan-out needed — this is a mechanical-but-careful merge task, work it
directly yourself.

**Branch:** `chore/sync-upstream-main` — already created off `origin/main`
(`c839e3280`, which has our 3 merged workflow-dispatch PRs).

**Remotes:** both `origin` (`joeysmithjrs/hermes-agent`, our fork) and `upstream`
(`NousResearch/hermes-agent`, the real project) are already configured and fetched in
this worktree.

## Goal

Merge `upstream/main` into this branch, resolving any conflicts, so the fork picks up
~2900 commits of upstream changes while preserving everything we've built on the fork
(the workflow dispatch package, the CCR/OpenRouter multi-model tooling, and everything
else already on `origin/main`). Open a PR back to `origin/main`. **Do not merge the PR
yourself** — the operator will review and merge.

## What's on the fork that must survive intact

- `workflow/` package (Phase 1 + 3 remediation passes: IR, verifier, driver, store,
  CLI soft-import, conditional-edge/skip-propagation logic) — brand new package, should
  not conflict with anything upstream touches unless upstream independently added a
  `workflow/` top-level package (check for this specifically — if it exists upstream
  with different content, STOP and report rather than guessing which to keep).
- `hermes_cli/main.py` — has ONE soft-import hunk registering the `workflow` subcommand.
  This file is a likely conflict site since it's a common integration point upstream
  also touches. Preserve the workflow soft-import wrapped in try/except; merge upstream's
  changes around it carefully — do not just take "ours" or "theirs" wholesale on this
  file, actually read the surrounding diff.
- `scripts/ccr_openrouter_models.py`, `docs/ccr-openrouter-*.md` — new files, should not
  conflict.
- `docs/proposals/workflow-dispatch/**` — new docs tree, should not conflict.
- `pyproject.toml` — check for version/dependency conflicts; merge additively where
  possible (union of dependencies) rather than picking one side if both sides add
  different deps.
- `tests/workflow/**` — new tests, should not conflict.
- Existing fork-only merge commits (`06e098fe7 Merge upstream NousResearch/hermes-agent
  main`, `8faa9a308 Add production deploy workflow`, `30f96e912 Add VPS provisioning
  script...`) show this fork has synced with upstream before and has its own deploy
  infrastructure (`.github/workflows/deploy*.yml` or similar) that is NOT part of
  upstream — preserve fork-specific CI/deploy workflow files; do not let an upstream
  merge silently delete or overwrite them with upstream's own (probably absent or
  different) deploy setup.

## Process

1. **Before merging anything**, run `git log --oneline origin/main..upstream/main | wc -l`
   and `git diff origin/main upstream/main --stat` to get a sense of surface area. Note
   anything that touches `workflow/`, `hermes_cli/main.py`, `toolsets.py`,
   `tools/delegate_tool.py`, `hermes_state.py`, `cron/jobs.py`, `gateway/`, or
   `.github/workflows/` specifically — these are the files our workflow-dispatch package
   and CCR tooling are most likely to collide with or depend on assumptions about.

2. **Check whether upstream independently built anything resembling workflow
   orchestration** (search for `workflow`, `dag`, `pipeline` as new top-level modules or
   CLI subcommands in the upstream diff). If upstream added something with a similar
   name/purpose, STOP and report the collision in detail rather than silently picking a
   side — this needs an operator decision, not an agent guess.

3. Merge: `git merge upstream/main` (prefer a real merge commit over rebase, to preserve
   both histories cleanly and make the eventual PR diff reviewable — do NOT squash or
   rebase the fork's existing commits).

4. Resolve conflicts file by file. For each conflict:
   - Read enough context to understand BOTH sides' intent before resolving — don't
     blindly take "ours" everywhere (that would silently drop real upstream improvements,
     bug fixes, and security patches) or "theirs" everywhere (that would break our
     workflow-dispatch integration points).
   - If a conflict is in a file we own entirely (anything under `workflow/`,
     `tests/workflow/`, `docs/proposals/workflow-dispatch/`, `scripts/ccr_openrouter_models.py`,
     `docs/ccr-openrouter-*.md`) and upstream doesn't independently have that exact
     path, there should be no real conflict — if there is one anyway (e.g. upstream added
     a file at the same path for something unrelated), STOP and report it.
   - If a conflict is in a shared integration file (`hermes_cli/main.py`, `toolsets.py`,
     `pyproject.toml`), merge both sides' additions — do not drop either.
   - If a conflict is in fork-specific infra (deploy workflows, VPS provisioning) that
     upstream doesn't have at all, keep ours as-is (upstream's absence of the file isn't
     really a "conflict" in a normal git merge — but double check nothing upstream added
     at the same path for a different purpose).

5. After the merge completes (or after resolving each conflicted file — do this
   incrementally, don't try to blind-resolve everything and check once at the end):
   - Run `pytest tests/workflow -q` — must still pass (66 tests as of this branch's base).
     If upstream changed something our workflow package depends on (`delegate_task`
     signature, `hermes_state` helpers, `toolsets.py` structure, `get_hermes_home`), our
     tests may need small adjustments — fix them if the fix is obviously correct and
     narrowly scoped to adapting to a genuine upstream API change; if the right fix is
     unclear, document it and move on rather than guessing.
   - Run the broader test suite if time/budget allows: check `AGENTS.md` or repo docs for
     the standard test command (likely `pytest` at the repo root, possibly with markers
     to skip slow/e2e tests) and run a reasonable subset — don't attempt the FULL suite
     if it's clearly multi-hour; use judgment on what's proportionate for a sync PR.
   - Run `ruff check --preview .` (matches CI's blocking lint rule, `PLW1514`
     unspecified-encoding) — fix any new violations introduced by the merge.
   - Confirm `hermes --help` still works (smoke test that the CLI didn't break):
     `python -c "import hermes_cli.main"` or equivalent import smoke test.
   - Confirm `import workflow; import workflow.cli` still works cleanly.

6. Write a summary to `docs/proposals/workflow-dispatch/UPSTREAM_SYNC_LOG.md` (new file):
   - How many commits merged from upstream.
   - Every file that had a real conflict, and how you resolved it (keep-ours /
     keep-theirs / merged-both, with a one-line reason each).
   - Any collision you flagged and stopped on (per step 2) instead of resolving yourself.
   - Test results (workflow suite + whatever broader subset you ran).
   - Anything upstream changed that our workflow package now depends on differently
     (e.g. if `delegate_task`'s signature changed, note it even if you didn't need to
     change our code because of it — future-proofing note for the next remediation pass).

7. Commit the merge (or the resolved conflict state — git will have already created the
   merge commit once conflicts are resolved and `git add`ed; just don't amend/squash it)
   plus the new `UPSTREAM_SYNC_LOG.md` as a follow-up commit if needed.

8. Push `chore/sync-upstream-main` to `origin`.

9. Open a PR from `chore/sync-upstream-main` to `origin/main` on `joeysmithjrs/hermes-agent`.
   Title: `chore: sync fork with upstream NousResearch/hermes-agent main`. Body: summary
   from `UPSTREAM_SYNC_LOG.md` plus test results. **DO NOT MERGE THE PR.**

## Non-negotiables

- Do not silently drop upstream bug fixes or security patches by blanket-preferring "ours".
- Do not silently break the workflow-dispatch package or CCR tooling by blanket-preferring
  "theirs".
- Do not rebase/squash the fork's existing commit history — use a real merge commit.
- Do not delete fork-specific deploy/CI infrastructure that has no upstream equivalent.
- Do not merge the PR yourself — operator reviews and merges.
- If you hit something genuinely ambiguous (both sides plausibly right, no clear correct
  resolution), STOP, document it clearly in `UPSTREAM_SYNC_LOG.md` under a "Needs operator
  decision" section, and either leave that specific conflict unresolved (if git allows
  committing progress on other files first) or resolve it with your best judgment AND
  flag it prominently — do not silently guess on something load-bearing without a flag.

## Stop condition

Stop when the merge is complete, workflow tests pass, `UPSTREAM_SYNC_LOG.md` is written,
and the PR is open — or when you hit a genuine blocking ambiguity that needs the operator,
in which case stop and report clearly rather than guessing on something important.
