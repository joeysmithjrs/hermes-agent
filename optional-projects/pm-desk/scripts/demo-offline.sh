#!/usr/bin/env bash
#
# PM Desk — full offline demo, PAPER ONLY.
#
# Drives the entire loop through the CLI with no network, no credentials and no
# LLM call:
#
#   store init → taxonomy compile → source collect v1 → source collect v2
#   → monitor evaluate → ingress serve + submit → workflow render
#   → workflow adjudicate → ledger list/show → ledger export
#
# Usage:  ./scripts/demo-offline.sh [desk-home]
#
set -euo pipefail

HOME_DIR="${1:-/tmp/pm-desk-demo}"
PORT="${PM_DESK_DEMO_PORT:-8788}"
CLI="npx tsx src/cli/pm-desk.ts"

# A throwaway secret for this demo run only. A real deployment exports its own
# and never writes it to disk.
export PM_DESK_INGRESS_SECRET
PM_DESK_INGRESS_SECRET="$(node -e "console.log(require('crypto').randomBytes(32).toString('hex'))")"

rm -rf "$HOME_DIR"
mkdir -p "$HOME_DIR"

banner() { printf '\n\033[1m=== %s ===\033[0m\n' "$1"; }

stop_server() {
  if [[ -n "${SERVER_PID:-}" ]]; then
    # `npx tsx` is a wrapper which spawns the actual Node server. Kill its
    # direct child first, then the wrapper, so an early demo failure never
    # leaves a stale loopback server holding the selected port.
    pkill -TERM -P "$SERVER_PID" 2>/dev/null || true
    kill "$SERVER_PID" 2>/dev/null || true
    wait "$SERVER_PID" 2>/dev/null || true
    unset SERVER_PID
  fi
}

cleanup() { stop_server; }
trap cleanup EXIT

banner "1. Initialise the local evidence store"
$CLI store init --home "$HOME_DIR"

banner "2. Compile a deterministic directive seed"
$CLI taxonomy compile --home "$HOME_DIR" --max-cards 3 --date 2026-07-30

banner "3. Validate the SourceSpec (no network touched)"
$CLI source validate --spec specs/sources/example_official_release.yaml --home "$HOME_DIR"

banner "4. Collect the primary source — fixture v1"
$CLI source collect \
  --spec specs/sources/example_official_release.yaml \
  --fixture fixtures/sources/example_official_release.v1.html \
  --home "$HOME_DIR"

banner "5. Collect again — fixture v2, the figure has been revised"
$CLI source collect \
  --spec specs/sources/example_official_release.yaml \
  --fixture fixtures/sources/example_official_release.v2.html \
  --home "$HOME_DIR"

banner "6. Evaluate the monitor (deterministic, no LLM)"
$CLI monitor evaluate \
  --spec specs/monitors/example_source_change.yaml \
  --home "$HOME_DIR" --json > "$HOME_DIR/signal.json"
head -c 600 "$HOME_DIR/signal.json"; echo

banner "7. Start the local ingress (127.0.0.1 only) and submit the signal"
# Use node directly rather than the npx/tsx wrapper: the direct process is the
# listener, so stop_server can reliably terminate it even if a preceding demo
# step fails.
PM_DESK_INGRESS_PORT="$PORT" node --import tsx src/cli/pm-desk.ts ingress serve --home "$HOME_DIR" > "$HOME_DIR/ingress.log" 2>&1 &
SERVER_PID=$!
for _ in $(seq 1 60); do
  curl -sf "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
  sleep 0.5
done
PM_DESK_INGRESS_PORT="$PORT" $CLI ingress submit --file "$HOME_DIR/signal.json" --home "$HOME_DIR"

banner "8. Inspect the durable outbox"
$CLI ingress outbox --home "$HOME_DIR"

stop_server
sleep 1

banner "9. Render the adjudication prompt (local render; no model is called)"
SIGNAL_ID=$(node -e "console.log(JSON.parse(require('fs').readFileSync('$HOME_DIR/signal.json','utf8')).signal_id)")
$CLI workflow render --signal "$SIGNAL_ID" --home "$HOME_DIR" | head -40

banner "10. Adjudicate — and watch the ledger REFUSE to invent an entry price"
# The sample monitor is bound to no market (no live token ships configured), so
# the signal carries no market snapshot. The ledger will not guess a price from
# nothing: this refusal is the invariant working, not a failure of the demo.
# The full adjudication -> ledger path with a real snapshot is covered offline
# by tests/e2e.test.ts, which supplies a market via the fake SDK client.
node -e "
  const fs = require('fs');
  const fixture = JSON.parse(fs.readFileSync('fixtures/adjudication/paper_alert.json','utf8'));
  delete fixture._comment;
  fixture.signal_id = '$SIGNAL_ID';
  fs.writeFileSync('$HOME_DIR/adjudication.json', JSON.stringify(fixture, null, 2));
"
if $CLI workflow adjudicate --result "$HOME_DIR/adjudication.json" --home "$HOME_DIR"; then
  echo "(recorded a ledger entry)"
else
  echo
  echo ">>> Expected: no ledger row was created, because no market observation"
  echo ">>> backs this signal. The desk refuses to fabricate an entry price."
fi

banner "11. Record a manual paper entry (the operator-acknowledged path)"
$CLI ledger record --manual \
  --thesis "Q1 GDP revised to 2.4% on the designated resolution source; watching for a linked market to bind." \
  --outcome YES --size 100 --slippage mid_no_slippage --mid 0.32 \
  --expiry-s 86400 --markout-s 300,3600 \
  --invalidation "a later estimate revises Q1 back to 3.0% or above" \
  --i-am-recording-a-paper-entry-manually \
  --home "$HOME_DIR"

$CLI ledger list --home "$HOME_DIR"

banner "12. Store status"
$CLI store status --home "$HOME_DIR"

printf '\n\033[1mDemo complete. PAPER ONLY — nothing here can trade.\033[0m\n'
printf 'Desk home: %s\n' "$HOME_DIR"
