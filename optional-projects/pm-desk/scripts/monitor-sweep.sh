#!/usr/bin/env bash
#
# PM Desk — deterministic monitor sweep. Script-first, no agent, no LLM.
#
# This is the body of the OPT-IN cron recipe in the README. It is deliberately
# a plain script rather than a scheduled agent turn:
#
#   - detection is pure code (predicate -> dedupe -> cooldown), so there is
#     nothing for a model to decide here;
#   - it prints NOTHING when nothing fired. Paired with
#     `hermes cron create --no-agent`, whose contract is "empty stdout = silent",
#     a quiet desk costs zero tokens and delivers zero notifications;
#   - it never starts a workflow. A fired signal goes to the desk's own local
#     ingress, which records it and queues it in the outbox. Adjudication stays
#     a separate, explicit step.
#
# Nothing here is installed or scheduled automatically. See the README section
# "Scheduling (opt-in)" for the exact `hermes cron create` command, which you
# run yourself.
#
# Environment:
#   PM_DESK_DIR             absolute path to the pm-desk package (required)
#   PM_DESK_HOME            desk store home (default: $PM_DESK_DIR/data)
#   PM_DESK_MONITOR_DIR     monitor specs (default: $PM_DESK_DIR/specs/monitors)
#   PM_DESK_INGRESS_SECRET  HMAC secret; only needed when the ingress is running
#   PM_DESK_INGRESS_PORT    default 8787
set -euo pipefail

: "${PM_DESK_DIR:?set PM_DESK_DIR to the absolute path of the pm-desk package}"
cd "$PM_DESK_DIR"

HOME_DIR="${PM_DESK_HOME:-$PM_DESK_DIR/data}"
MONITOR_DIR="${PM_DESK_MONITOR_DIR:-$PM_DESK_DIR/specs/monitors}"
CLI=(node --import tsx "$PM_DESK_DIR/src/cli/pm-desk.ts")

signals=$("${CLI[@]}" monitor evaluate --dir "$MONITOR_DIR" --home "$HOME_DIR" --json)

# `--json` prints `[]` when nothing fired, a single object for one signal, or an
# array for several. Normalise to a count without needing jq.
count=$(printf '%s' "$signals" | node -e '
  let s = "";
  process.stdin.on("data", (d) => (s += d));
  process.stdin.on("end", () => {
    const v = JSON.parse(s || "[]");
    process.stdout.write(String(Array.isArray(v) ? v.length : 1));
  });
')

# Silent is the common case and the whole point. No output, no notification.
[ "$count" = "0" ] && exit 0

printf 'PM DESK — %s monitor signal(s) fired (PAPER ONLY, no order can result)\n' "$count"

# Hand each one to the local loopback ingress if it is up: it validates the
# envelope, records BEFORE dispatching, and enforces idempotency. If it is not
# running, the signal is already durably recorded in the store by the evaluation
# above — say so rather than failing the job.
port="${PM_DESK_INGRESS_PORT:-8787}"
if curl -sf "http://127.0.0.1:${port}/health" >/dev/null 2>&1; then
  printf '%s' "$signals" > "${TMPDIR:-/tmp}/pm-desk-sweep-$$.json"
  "${CLI[@]}" ingress submit --file "${TMPDIR:-/tmp}/pm-desk-sweep-$$.json" --home "$HOME_DIR"
  rm -f "${TMPDIR:-/tmp}/pm-desk-sweep-$$.json"
else
  printf 'local ingress not running on 127.0.0.1:%s — signals are recorded in the store; inspect with `pm-desk ingress outbox`\n' "$port"
fi

printf '%s\n' "$signals"
