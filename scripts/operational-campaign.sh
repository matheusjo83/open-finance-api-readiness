#!/usr/bin/env bash
# An operational campaign (make operational-campaign): for each seed 1..SEEDS, a 90-day history at
# the CI volume (make history) and the operational job over it with --check, writing its
# detection units; the last run also writes one detection report over all the runs, gated, named
# <name>. The report is printed and stays in s3a://lakehouse/reports/<name>/operational.md.
# Usage: operational-campaign.sh <seeds> <name>
set -euo pipefail

seeds=$1 name=$2
runs=()
for seed in $(seq 1 "$seeds"); do
  runs+=("history-s$seed-90d-30x3")
done
all=$(IFS=,; echo "${runs[*]}")

for seed in $(seq 1 "$seeds"); do
  run_id="history-s$seed-90d-30x3"
  echo "=== $run_id"
  make --no-print-directory history HISTORY_SEED="$seed"
  args='"--check", "--report", "'"$name-seed$seed"'", '
  if [ "$seed" -eq "$seeds" ]; then
    args='"--check", "--report", "'"$name"'", "--report-runs", "'"$all"'", "--gate", '
  fi
  make --no-print-directory _operational RUN_ID="$run_id" OPERATIONAL_ARGS="$args"
done

kubectl -n spark logs operational-driver | sed -n '/^# Operational detection report/,/^OPERATIONAL_REPORT /p'
