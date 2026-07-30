#!/usr/bin/env bash
# Post-Phase-3 CLI surfaces: the versioned catalog, and loop-back via restart.
#
# Nothing here is a new scheduler, store or transport — the catalog is a file
# tree under $HERMES_HOME/workflows/catalog/, and loop-back is an ordinary new
# run that records where it came from.
set -euo pipefail

WF="${1:-docs/proposals/workflow-dispatch/examples-post-phase3-debate.yaml}"

# ---------------------------------------------------------------------------
# 1. Catalog — register a workflow as a versioned recipe
# ---------------------------------------------------------------------------
# register SNAPSHOTS the YAML; it deliberately does not compile it, because a
# parameterized recipe (`{{ params.* }}` placeholders) only resolves at
# run-catalog time. run-catalog compiles AND verifies before anything executes.
hermes workflow register \
  --id desk-council \
  --from-file "$WF" \
  --tags desk,debate \
  --owner joe \
  --description "Three-voice desk council with judge escalation"

# Re-registering the same id writes version 2, 3, ... — previous versions stay
# on disk and stay runnable. Nothing overwrites a registered recipe in place.
hermes workflow register --id desk-council --from-file "$WF"

hermes workflow list-catalog --tags desk            # latest per id
hermes workflow list-catalog --id desk-council --all-versions --json

# Run the latest, or pin a version. --params fills the recipe's placeholders.
hermes workflow run-catalog desk-council --fake
hermes workflow run-catalog desk-council --version 1 --params '{"market": "energy"}' --dry-run

# ---------------------------------------------------------------------------
# 2. Loop-back — feed a finished run's result into a new run
# ---------------------------------------------------------------------------
# The graph stays ACYCLIC. A backward route is a NEW run_id that records its
# lineage (`from_run`), so the previous run's artifacts and checkpoint are
# untouched and the history stays reconstructable.

# `run` already prints the envelope as JSON on stdout (the worker banner goes
# to stderr), so no --json flag is needed here.
RUN_ID="$(hermes workflow run "$WF" --fake 2>/dev/null | python -c 'import json,sys; print(json.load(sys.stdin)["run_id"])')"

# restart: re-run the SAME workflow that run used, seeded from its own output.
# No path argument — the definition comes from the source run.
hermes workflow restart "$RUN_ID" --select succeeded --as previous_nodes --fake

# Seed from a DIFFERENT run than the one being restarted:
#   hermes workflow restart "$RUN_ID" --input-from-run "$OTHER_RUN_ID"
#
# chain: same mechanism, but you name the target workflow explicitly (use this
# when the loop goes to a different workflow than the source ran).
#   hermes workflow chain "$RUN_ID" other-workflow.yaml --select status --as prev
#
# run --from-run: the inline form of chain, for when you already have the path.
#   hermes workflow run "$WF" --from-run "$RUN_ID"

# The lineage is on the run record, the envelope and `status`:
#   hermes workflow status <new_run_id> | python -c \
#     'import json,sys; print(json.load(sys.stdin)["from_run"])'
#   -> {"run_id": "...", "workflow_id": "...", "status": "succeeded",
#       "select": "succeeded", "as": "previous_nodes"}

# ---------------------------------------------------------------------------
# 3. Resuming, not looping
# ---------------------------------------------------------------------------
# A loop-back starts a NEW run. To continue the SAME run from a node, resume:
#   hermes workflow run --resume "$RUN_ID" --from-node desk_debate --retry-failed
