#!/usr/bin/env node
import { isPmDeskError, UsageError } from '../core/errors.js';
import { Flags, parseArgs } from './args.js';
import { marketCommand } from './commands/market.js';
import { storeCommand } from './commands/store.js';

const HELP = `pm-desk — local-first, PAPER-ONLY prediction-market research desk

This tool cannot trade, sign, connect a wallet or place an order. Every command
reads public data or local artifacts and writes only to the local desk store.

USAGE
  pm-desk <command> <subcommand> [flags]

STORE
  store init                      Create/migrate the local evidence store
  store status                    Row counts per table
  store export --table <name>     Dump a table as JSON (--limit N)

MARKET DATA (official Polymarket public client, read-only)
  market discover --limit N       Discover active markets (--dry-run, --json)
  market snapshot --token <id>    Append a BBO/midpoint observation (repeatable)
  market list                     List stored open markets
  market capability               Report realtime/subscription capability

SOURCES (deterministic Browserbase collection)
  source validate --spec <file>   Validate a SourceSpec without any network use
  source collect --spec <file>    Collect (--fixture <file>, --dry-run, or --live)

MONITOR (no LLM in this path)
  monitor evaluate --spec <file>  Evaluate a rule; prints SILENT or a signal
  monitor list --dir <dir>        List monitor specs

INGRESS (local only, 127.0.0.1)
  ingress serve                   Start the local signal ingress
  ingress submit --file <file>    HMAC-sign and POST an envelope (test client)
  ingress outbox                  Inspect queued signals

TAXONOMY
  taxonomy compile                Deterministic directive seed
                                  --mode mission_x_arena (default): one mission
                                    across N Polymarket arenas — the morning shape
                                  --mode stratify_families: one card per family
                                  --mission <family|card>  force the mission
                                  --include-explore        spend a slot on exploration
  taxonomy list                   Every card, family status and mission eligibility
  taxonomy arenas                 The arena set the fanout samples from

LEDGER (paper only)
  ledger record --adjudication <file>   Record from a validated paper_alert
  ledger record --manual ...            Explicit operator entry
  ledger list | ledger show --entry <id> | ledger export
  ledger annotate --entry <id>          Add a markout/outcome/note
  ledger render --entry <id>            Telegram-ready PAPER ONLY alert

WORKFLOW
  workflow render --signal <id>   Render the adjudication prompt (no LLM call)
  workflow adjudicate --signal <id> --result <file>   Record a result

EXECUTION PLAN (what the morning generator emits and the gate approves)
  plan validate --file <f>        Validate an ExecutionPlan
  plan show --file <f>            Full plan, human-readable
  plan render-telegram --file <f> The approval message, commands verbatim
  plan schema                     JSON Schema for the plan
  plan from-run --run-id <id>     Extract the plan from a Hermes run (--out <f>)
  plan approve --file <f> --run-id <id>   Stamp from Hermes' recorded gate decision

PROVISION (deterministic; installs what an approved plan describes)
  provision dry-run --plan <f>    Print every file and command, write nothing
  provision apply --plan <f>      Install (needs --i-approved-this-plan)
  provision status --plan-id <id> What is installed for a plan
  provision revoke --plan-id <id> Remove this plan's cron jobs

HERMES (optional binding; dry-run by default, never edits Hermes config)
  hermes workflows                Print the shipped workflows and their paths
  hermes install-prompts          Plan the prompt-library install (--apply to write)

RESEARCH (paper only; edge detector, never an order)
  research cpi-calibrate --nowcasts <csv> --prints <csv>
                                  P(BLS CPI YoY print rounds into a 0.1% bucket),
                                  compared to a Polymarket mid (--mid). Fail-closed
                                  if the no-look-ahead sample is too small.
                                  --live-nowcast 3.42 --bucket 3.4 --as-of YYYY-MM-DD
                                  --json  (live fetch needs PM_DESK_LIVE_CPI=1)

GLOBAL FLAGS
  --home <dir>   Desk home (default $PM_DESK_HOME or ./data)
  --json         Machine-readable output
  --help         Show this help
`;

type Handler = (sub: string | undefined, flags: Flags) => Promise<number>;

async function loadHandler(command: string): Promise<Handler | undefined> {
  switch (command) {
    case 'store':
      return storeCommand;
    case 'market':
      return marketCommand;
    case 'source':
      return (await import('./commands/source.js')).sourceCommand;
    case 'monitor':
      return (await import('./commands/monitor.js')).monitorCommand;
    case 'ingress':
      return (await import('./commands/ingress.js')).ingressCommand;
    case 'ledger':
      return (await import('./commands/ledger.js')).ledgerCommand;
    case 'taxonomy':
      return (await import('./commands/taxonomy.js')).taxonomyCommand;
    case 'workflow':
      return (await import('./commands/workflow.js')).workflowCommand;
    case 'plan':
      return (await import('./commands/plan.js')).planCommand;
    case 'provision':
      return (await import('./commands/provision.js')).provisionCommand;
    case 'hermes':
      return (await import('./commands/hermes.js')).hermesCommand;
    case 'research':
      return (await import('./commands/research.js')).researchCommand;
    default:
      return undefined;
  }
}

export async function run(argv: readonly string[]): Promise<number> {
  const parsed = parseArgs(argv);
  const flags = new Flags(parsed);
  const [command, sub] = parsed.positionals;

  if (!command || flags.bool('help')) {
    process.stdout.write(HELP);
    return command ? 0 : 1;
  }

  const handler = await loadHandler(command);
  if (!handler) {
    throw new UsageError(`unknown command: ${command}`, {
      hint: 'Run `pm-desk --help` for the command list.',
    });
  }
  return handler(sub, flags);
}

const isDirectRun =
  process.argv[1] !== undefined &&
  (process.argv[1].endsWith('pm-desk.ts') || process.argv[1].endsWith('pm-desk.js'));

if (isDirectRun) {
  run(process.argv.slice(2))
    .then((code) => {
      process.exitCode = code;
    })
    .catch((err: unknown) => {
      if (isPmDeskError(err)) {
        process.stderr.write(`${err.toCliString()}\n`);
      } else {
        process.stderr.write(
          `UNEXPECTED_ERROR: ${err instanceof Error ? err.message : String(err)}\n`,
        );
        if (process.env.PM_DESK_DEBUG === '1' && err instanceof Error && err.stack) {
          process.stderr.write(`${err.stack}\n`);
        }
      }
      process.exitCode = 1;
    });
}
