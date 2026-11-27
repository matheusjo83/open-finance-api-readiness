#!/usr/bin/env bash
# One run of the limits layer (docs/limits.md): the limits proxy with DEFECT_SEED=<seed> and a run
# id of its own, a fresh transmitter (the load's origins are the same in every run, so both
# start from zero counters), then the k6 Job and the upload of its log.
# Usage: limits-run.sh <seed> [run id]
set -euo pipefail

seed=$1
run_id=${2:-limits-s${seed}-$(date -u +%Y%m%dT%H%M%SZ)}

kubectl -n ofb set env deploy/proxy-limits DEFECT_SEED="$seed" RUN_ID="$run_id" >/dev/null
kubectl -n ofb rollout restart deploy/transmitter >/dev/null
kubectl -n ofb rollout status deploy/transmitter --timeout=3m >/dev/null
kubectl -n ofb rollout status deploy/proxy-limits --timeout=3m >/dev/null

# Right after a rollout a Service may still route to the old pod: wait until both answer from
# the new ones several times in a row (the proxy's run id, the transmitter's instance).
instance=$(kubectl -n ofb exec deploy/transmitter -- python -c \
  "import json, urllib.request; print(json.load(urllib.request.urlopen('http://localhost:8080/healthz'))['instance'])")
kubectl -n ofb exec -i deploy/proxy-limits -- python - "$run_id" "$instance" <<'PY'
import json, sys, time, urllib.request

def get(url, key):
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.load(r)[key]
    except Exception:
        return None

expected = {("http://proxy-limits.ofb.svc/_proxy/run", "run_id"): sys.argv[1],
            ("http://transmitter.ofb.svc/healthz", "instance"): sys.argv[2]}
deadline, seen = time.monotonic() + 90, 0
while seen < 5:
    seen = seen + 1 if all(get(url, key) == value for (url, key), value in expected.items()) else 0
    if time.monotonic() > deadline:
        sys.exit("the Services do not answer from the new pods after 90 s")
    time.sleep(0.5)
PY

kubectl -n ofb create configmap limits-k6 --from-file=limits.js=infra/k6/limits.js \
  --dry-run=client -o yaml | kubectl apply -f - >/dev/null
kubectl -n ofb delete job limits-k6 --ignore-not-found --wait
kubectl apply -f infra/k8s/limits-k6.yaml
scripts/wait-job.sh ofb limits-k6 600
echo "limits run $run_id (seed $seed)"
