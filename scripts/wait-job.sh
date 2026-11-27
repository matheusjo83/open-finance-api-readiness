#!/usr/bin/env bash
# Waits for a Job to finish, prints its pod log and exits 0 only if it succeeded.
# Usage: wait-job.sh <namespace> <name> <timeout-seconds>
set -euo pipefail

ns=$1 name=$2 timeout=$3
deadline=$((SECONDS + timeout))
state=""

while ((SECONDS < deadline)); do
  state=$(kubectl -n "$ns" get job "$name" \
    -o jsonpath='{range .status.conditions[?(@.status=="True")]}{.type}{" "}{end}' 2>/dev/null || true)
  case " $state " in
    *" Complete "* | *" Failed "*) break ;;
  esac
  sleep 2
done

kubectl -n "$ns" logs "job/$name" --all-containers --tail=200 || true
echo "Job $ns/$name: ${state:-no condition after ${timeout}s}"
[[ " $state " == *" Complete "* ]]
