#!/usr/bin/env bash
#
# Carry-forward proof: every tracked file from the standalone PM Desk repo is
# present, byte-identical, in this package.
#
# The desk was developed in a standalone repository and transplanted into the
# Hermes fork as a single commit, then relocated under `optional-projects/`.
# Two things could silently go wrong in that sequence: a file could be dropped,
# or a later standalone commit could be left behind. This script proves neither
# happened, by comparing git blob hashes — not timestamps, not file counts.
#
# Usage:
#   ./scripts/verify-carry-forward.sh [source-repo] [source-ref]
#
# Defaults: /home/hermes/pm-desk at feat/pm-desk-mvp — the machine this was
# transplanted on. Exits 0 and skips (not fails) when the source repo is not
# present, so the check is a no-op on any other checkout, including CI.
#
# Only *tracked* files are compared. node_modules/, data/, dist/, logs/ and
# .env are untracked runtime state in both trees by design and are out of scope.
set -euo pipefail

SOURCE_REPO="${1:-/home/hermes/pm-desk}"
SOURCE_REF="${2:-feat/pm-desk-mvp}"

FORK_REPO="$(git -C "$(dirname "${BASH_SOURCE[0]}")" rev-parse --show-toplevel)"
PACKAGE_PREFIX="optional-projects/pm-desk"

if [[ ! -d "$SOURCE_REPO/.git" ]]; then
  echo "SKIP: no standalone source repo at $SOURCE_REPO"
  echo "      (this proof only runs on the machine the transplant was made from)"
  exit 0
fi

echo "source : $SOURCE_REPO @ $SOURCE_REF"
echo "fork   : $FORK_REPO @ $PACKAGE_PREFIX (HEAD)"
echo

# Path sets. `git ls-tree -r --name-only` reads the committed tree, so an
# uncommitted local edit can never make this pass. LC_ALL=C so `sort` and `comm`
# agree on collation — a locale-sorted list makes comm emit garbage silently.
git -C "$SOURCE_REPO" ls-tree -r --name-only "$SOURCE_REF" | LC_ALL=C sort > /tmp/cf-source-paths.txt
git -C "$FORK_REPO" ls-tree -r --name-only "HEAD:$PACKAGE_PREFIX" | LC_ALL=C sort > /tmp/cf-fork-paths.txt

missing=$(LC_ALL=C comm -23 /tmp/cf-source-paths.txt /tmp/cf-fork-paths.txt)
added=$(LC_ALL=C comm -13 /tmp/cf-source-paths.txt /tmp/cf-fork-paths.txt)

status=0

if [[ -n "$missing" ]]; then
  echo "FAIL: tracked in the standalone repo but absent here:"
  printf '  %s\n' $missing
  status=1
else
  echo "OK: all $(wc -l < /tmp/cf-source-paths.txt) standalone tracked paths are present"
fi

if [[ -n "$added" ]]; then
  # Additions are expected — this is the integration work. Listed, not failed.
  echo
  echo "INFO: added during Hermes integration (expected):"
  printf '  %s\n' $added
fi

# Content. Compare git blob hashes of the committed trees on both sides.
echo
differs=0
while read -r path; do
  # --verify -q: without it, rev-parse echoes the failed argument to stdout and
  # the comparison silently reads a path string as if it were a blob hash.
  a=$(git -C "$SOURCE_REPO" rev-parse --verify -q "$SOURCE_REF:$path" || echo absent-in-source)
  b=$(git -C "$FORK_REPO" rev-parse --verify -q "HEAD:$PACKAGE_PREFIX/$path" || echo absent-in-fork)
  if [[ "$a" != "$b" ]]; then
    echo "  DIFFERS: $path"
    echo "      standalone $a"
    echo "      fork       $b"
    differs=$((differs + 1))
  fi
done < /tmp/cf-source-paths.txt

if [[ "$differs" -gt 0 ]]; then
  echo "FAIL: $differs file(s) differ from the standalone tree"
  echo
  echo "If a difference is intentional (an integration fix applied on top),"
  echo "record it in INTEGRATION_SUMMARY.md rather than silencing this check."
  status=1
else
  echo "OK: every carried-forward file is byte-identical (git blob hash match)"
fi

# The specific standalone commit the transplant is known to have post-dated.
echo
if git -C "$FORK_REPO" show "HEAD:$PACKAGE_PREFIX/scripts/demo-offline.sh" | grep -q 'stop_server'; then
  echo "OK: 266131d demo cleanup (stop_server / direct node listener) is present"
else
  echo "FAIL: 266131d demo cleanup is missing from scripts/demo-offline.sh"
  status=1
fi

exit "$status"
