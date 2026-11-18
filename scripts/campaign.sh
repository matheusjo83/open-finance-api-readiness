#!/usr/bin/env bash
# A detection campaign (make campaign): for each defect seed 1..SEEDS, the proxy gets that seed
# and a run id <name>-seed<k>, the transmitter CUSTOMERS customers; the validator walks it and
# the business-rules job checks it. Then one report over all the runs, named <name>. The default
# settings (seed 42, 5 customers, generated run id) are restored at the end, even on failure.
# Usage: campaign.sh <seeds> <customers> <name>
set -euo pipefail

seeds=$1 customers=$2 name=$3
restore() {
  kubectl -n ofb set env deploy/proxy DEFECT_SEED=42 RUN_ID- >/dev/null
  kubectl -n ofb set env deploy/transmitter TRANSMITTER_CUSTOMERS=5 >/dev/null
  kubectl -n ofb rollout status deploy/transmitter --timeout=3m >/dev/null
  kubectl -n ofb rollout status deploy/proxy --timeout=3m >/dev/null
}
trap restore EXIT

kubectl -n ofb set env deploy/transmitter TRANSMITTER_CUSTOMERS="$customers" >/dev/null
kubectl -n ofb rollout status deploy/transmitter --timeout=3m

runs=()
for seed in $(seq 1 "$seeds"); do
  run_id="$name-seed$seed"
  echo "=== $run_id"
  kubectl -n ofb set env deploy/proxy DEFECT_SEED="$seed" RUN_ID="$run_id" >/dev/null
  kubectl -n ofb rollout status deploy/proxy --timeout=3m
  make --no-print-directory validate
  make --no-print-directory business-rules RUN_ID="$run_id"
  runs+=("$run_id")
done

make --no-print-directory report RUNS="$(IFS=,; echo "${runs[*]}")" REPORT_NAME="$name"
