#!/usr/bin/env bash
# A limits campaign (make limits-campaign): for each seed 1..SEEDS, a limits run (the limits proxy
# with that seed, a fresh transmitter, k6) and the limits job over it, writing its detection
# units; the last run also writes one detection report over all the runs, gated, named <name>. The
# report is printed and stays in s3a://lakehouse/reports/<name>/limits.md.
# Usage: limits-campaign.sh <seeds> <name>
set -euo pipefail

seeds=$1 name=$2
# A run id of its own per run (the time in it): a campaign run again under the same name does
# not write over the parts of an earlier one.
stamp=$(date -u +%H%M%S)
runs=()

make --no-print-directory validator-image
for seed in $(seq 1 "$seeds"); do
  run_id="limits-s$seed-$name-$stamp"
  runs+=("$run_id")
  echo "=== $run_id"
  scripts/limits-run.sh "$seed" "$run_id"
  make --no-print-directory limits-smoke
  args='"--report", "'"$name-seed$seed"'", '
  if [ "$seed" -eq "$seeds" ]; then
    all=$(IFS=,; echo "${runs[*]}")
    args='"--report", "'"$name"'", "--report-runs", "'"$all"'", "--gate", '
  fi
  make --no-print-directory _limits RUN_ID="$run_id" LIMITS_ARGS="$args"
done

kubectl -n spark logs limits-driver | sed -n '/^# Limits detection report/,/^LIMITS_REPORT /p'
