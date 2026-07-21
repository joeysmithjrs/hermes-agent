# Deploy / VPS provisioning

This directory holds infra scripts for *this fork's* production VPS setup —
things that live alongside `hermes-agent` but aren't part of the Hermes
framework itself, so they don't belong in `scripts/` (that's upstream's own
build/lint/release tooling).

## What's here

- **`setup-claude-code-router.sh`** — provisions
  [claude-code-router](https://github.com/musistudio/claude-code-router)
  (CCR) so the Claude Code CLI, when Hermes shells out to it via the
  `claude-code` skill, routes through OpenRouter (default: `z-ai/glm-5.2`)
  instead of Anthropic's own API. Installs CCR + `@anthropic-ai/claude-code`
  under the `hermes` user, runs CCR as a systemd service
  (`ccr-router.service`), and points Claude Code's own `~/.claude/settings.json`
  at it.

  Run manually, as root, on the VPS — **not** wired into `.github/workflows/deploy.yml`
  or the CI deploy key on purpose: it does npm-global installs and systemd
  work that has no reason to re-run on every `git push`, unlike the routine
  `hermes update` the deploy pipeline does. Re-run it after a server rebuild,
  after rotating `OPENROUTER_API_KEY`, or to change the model:

  ```bash
  scp deploy/setup-claude-code-router.sh root@<host>:/root/
  ssh root@<host> '/root/setup-claude-code-router.sh z-ai/glm-5.2'
  ```

  Idempotent — safe to re-run. Reuses the existing CCR client key and
  management token rather than rotating them each time.

## Why this exists instead of a native Hermes config

Claude Code CLI is Anthropic's own binary, not Hermes code — Hermes's
`model.provider`/`delegation.*` config only governs calls Hermes's own
process makes for itself, and Hermes's subprocess sandbox unconditionally
strips `ANTHROPIC_API_KEY`/`ANTHROPIC_BASE_URL`/`OPENROUTER_API_KEY` from
anything it spawns via the `terminal` tool (see `tools/environments/local.py`,
GHSA-rhgp-j443-p4rf) — that's a hard, non-configurable security boundary,
not a gap you can close from `config.yaml`. Hermes's own native local-proxy
tool (`hermes proxy`, see `website/docs/user-guide/features/subscription-proxy.md`)
explicitly documents that Anthropic-Messages-format translation is out of
scope for it. CCR is what fills both gaps: it manages Claude Code's own
`settings.json` (a file Claude Code reads for itself, unaffected by Hermes's
subprocess scrubbing) and translates between the Anthropic Messages API and
OpenRouter's format.

## A known fragility, for whoever touches this next

CCR (as of the version installed 2026-07) has no CLI or documented API for
configuring providers/models/profiles outside its browser-based setup UI —
and that UI's tab navigation doesn't function when CCR runs headlessly
outside its Electron desktop shell (reproducible, not an environment fluke;
confirmed via network/DOM inspection during setup). `setup-claude-code-router.sh`
works around this by writing CCR's `~/.claude-code-router/config.sqlite`
directly, having reverse-engineered its current JSON shape. If a CCR
upgrade changes that shape, this script's config-bootstrap step will need
re-deriving — dump `app_config.value_json` from a fresh install's sqlite
file and diff against what this script writes.
