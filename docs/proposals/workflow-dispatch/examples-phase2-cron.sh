#!/usr/bin/env bash
# Cron recipe: run a Hermes workflow from a `no_agent` scheduled job.
#
#   hermes cron create --name morning-brief --schedule "0 7 * * *" \
#     --no-agent --script "$HERMES_HOME/scripts/run_workflow.sh morning_brief.yaml" \
#     --deliver telegram
#
# The tick itself spends no tokens — only the workflow's agent nodes do.
# Copy to $HERMES_HOME/scripts/run_workflow.sh and chmod +x.
set -euo pipefail

WORKFLOW="${1:?usage: run_workflow.sh <workflow.yaml> [json-input]}"
INPUT="${2:-}"

args=(workflow run "$WORKFLOW")
[ -n "$INPUT" ] && args+=(--input "$INPUT")

set +e
output="$(hermes "${args[@]}" 2>&1)"
rc=$?
set -e

# `hermes workflow run` exit codes: 0 ok / 1 runtime fail / 2 verify reject
# / 3 usage / 4 awaiting gate. Code 4 is a CORRECT outcome (a human gate
# parked the run) — do not let the scheduler report it as a failure.
case "$rc" in
  0) echo "workflow ok"; echo "$output" ;;
  4) echo "workflow parked at a gate — approval needed"
     echo "$output"
     echo "approve with: hermes workflow gate <run_id> <gate_id> --decide approve"
     rc=0 ;;
  2) echo "workflow REJECTED by the verifier (definition error, not a run failure)"
     echo "$output" ;;
  *) echo "workflow failed (exit $rc)"; echo "$output" ;;
esac

exit "$rc"
