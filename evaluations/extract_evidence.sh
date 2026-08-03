#!/usr/bin/env bash
# Pull evaluation run evidence out of a DTU onto the host.
#
# WHY THIS EXISTS
#   harness.py derives EVIDENCE_ROOT from its own location, so when the harness
#   runs inside the DTU (which it must -- the host attractor is upstream and
#   trips the root source_dir bug) every run directory is written to
#   /workspace/.work/evaluations/expert-builder/ and dies with the container.
#   A prior matrix was lost exactly this way: the numbers were reported, the
#   artifacts were not kept, and the result became unreviewable.
#
#   This is deliberately NOT a change to harness.py. The instrument stays
#   byte-identical to the one that produced earlier numbers; extraction is a
#   separate concern layered outside it.
#
# Usage:
#   ./extract_evidence.sh <instance> [dest]
#
#   instance  DTU instance id/name (e.g. ebeval)
#   dest      host directory (default: <workspace>/.work/evaluations/expert-builder)

set -euo pipefail

INSTANCE="${1:?usage: extract_evidence.sh <instance> [dest]}"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WORKSPACE_ROOT="$(cd "$HERE/../.." && pwd)"
DEST="${2:-$WORKSPACE_ROOT/.work/evaluations/expert-builder}"

REMOTE_ROOT="/workspace/.work/evaluations/expert-builder"

mkdir -p "$DEST"

# List run directories inside the container. Missing root is not an error --
# it just means no run has produced evidence yet.
#
# `exec` returns a JSON envelope, not raw output, so the command's stdout has to
# be unwrapped before it can be read as a list of directory names.
runs="$(amplifier-digital-twin exec "$INSTANCE" -- \
          bash -lc "ls -1 $REMOTE_ROOT 2>/dev/null || true" \
        | jq -r '.stdout' | tr -d '\r')"

if [ -z "$runs" ]; then
  echo "extract: no run directories under $REMOTE_ROOT in $INSTANCE"
  exit 0
fi

pulled=0
skipped=0
while IFS= read -r run; do
  [ -n "$run" ] || continue

  # Never overwrite evidence already on the host: a completed run directory is
  # immutable, and a silent re-pull would mask a mismatch rather than surface it.
  if [ -e "$DEST/$run" ]; then
    echo "extract: skip $run (already on host)"
    skipped=$((skipped + 1))
    continue
  fi

  echo "extract: pulling $run ..."
  # Destination is the PARENT: file-pull places the source directory inside the
  # destination, so passing "$DEST/$run" would nest it as "$DEST/$run/$run".
  amplifier-digital-twin file-pull "$INSTANCE" -r \
      "$REMOTE_ROOT/$run" "$DEST/" >/dev/null
  pulled=$((pulled + 1))
done <<< "$runs"

echo "extract: $pulled pulled, $skipped already present -> $DEST"

# Report what actually landed, so a caller can verify rather than assume.
for run in "$DEST"/*/; do
  [ -d "$run" ] || continue
  name="$(basename "$run")"
  if [ -f "$run/result.json" ]; then
    summary="$(jq -r '
        "exit=\(.pipeline.exit_code // "?")"
      + "  C1=\(.C1_fidelity.passed // "?")/\(.C1_fidelity.total // "?")"
      + "  R2_violated=\(.R2_reference_integrity.violated)"
    ' "$run/result.json" 2>/dev/null || echo "result.json unreadable")"
    echo "  $name  $summary"
  else
    echo "  $name  (no result.json -- run incomplete)"
  fi
done
