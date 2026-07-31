# optional-projects/

Self-contained projects that ship **in this repository but are not part of any
Hermes build, install, or test path**. Nothing here is installed, imported, or
executed by Hermes itself. A user who never opens this directory pays nothing
for it.

This is the same idea as `optional-skills/` and `optional-mcps/` — shipped,
discoverable, off by default — applied to whole projects rather than to a skill
or an MCP server.

## The contract

A project under `optional-projects/<name>/` must:

1. **Own its dependencies.** It is deliberately *not* an npm workspace (see
   `workspaces` in the root `package.json`) and must not be added to one. A
   root `npm ci` must not install a single byte on its behalf. It has its own
   `package.json` / lockfile and its own `npm ci`.
2. **Own its toolchain requirements.** If it needs a different Node major than
   the root project's `engines.node`, that is its business and it declares it
   itself — with `engine-strict` and a preflight, not a warning.
3. **Add zero core surface.** No model tool, no toolset entry, no
   `tools/*.py` registration, no gateway platform, no change to the system
   prompt. Per `AGENTS.md`, the core is a narrow waist; a vendor-facing project
   is about as far out at the edge as it gets.
4. **Own its CI.** A dedicated, path-filtered workflow that runs only when the
   project's files change. It must not extend a root lane, and a failure in it
   must not be caused by (or cause) a failure in Hermes' own checks.
5. **Ignore its own runtime state.** Databases, artifacts, logs, `.env`,
   `node_modules/` and build output are covered by the project's own
   `.gitignore`.

## Why not `plugins/`

`AGENTS.md` (June 2026 policy) closes `plugins/` to third-party-product and
vendor-SaaS integrations: they are a standing maintenance cost against a
fast-moving core, for a backend we do not own. A project here is not a plugin —
it does not implement a Hermes ABC, is not discovered by the plugin loader, and
cannot be loaded into the agent. It is an independent program that happens to
be versioned alongside Hermes and to know how to talk to `hermes` as a CLI.

## Projects

| Project | What it is | Node | Run it |
|---|---|---|---|
| [`pm-desk/`](pm-desk/) | Local-first, **paper-only** prediction-market research and alerting desk. Cannot trade, sign, or hold a wallet. | >= 24 | `cd optional-projects/pm-desk && npm ci && npm run check` |
