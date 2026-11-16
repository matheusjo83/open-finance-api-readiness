#!/usr/bin/env bash
# Waits for a SparkApplication to finish, prints its driver log and exits 0 only if it COMPLETED.
# Usage: wait-sparkapp.sh <namespace> <name> <timeout-seconds>
set -euo pipefail

ns=$1 name=$2 timeout=$3
deadline=$((SECONDS + timeout))
state=""

while ((SECONDS < deadline)); do
  state=$(kubectl -n "$ns" get sparkapplication "$name" -o jsonpath='{.status.applicationState.state}' 2>/dev/null || true)
  case $state in
    COMPLETED | FAILED | SUBMISSION_FAILED) break ;;
  esac
  sleep 5
done

driver=$(kubectl -n "$ns" get sparkapplication "$name" -o jsonpath='{.status.driverInfo.podName}' 2>/dev/null || true)
if [[ -n $driver ]]; then
  kubectl -n "$ns" logs "$driver" --tail=200 || true
fi

echo "SparkApplication $ns/$name: ${state:-no state after ${timeout}s}"
[[ $state == COMPLETED ]]
