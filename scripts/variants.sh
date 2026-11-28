#!/usr/bin/env bash
# The security layer's variants (make variants, decision 51): for each one, Keycloak and the
# transmitter with that defect (make security-deploy VARIANT=<id>), one run of the suite's
# plan, its CONFORMANCE line kept; then the clean baseline again, even on failure, and the
# matrix of what detected each variant. The id `baseline` is a clean run among them (the suite's
# noise: anything unexpected there is not the variants').
# Usage: variants.sh "<id> <id> ..." <name>
set -euo pipefail

ids=$1 name=$2
out=reports/variants
mkdir -p "$out"
log="$out/$name.log"
: > "$log"
: > "$out/$name.modules.log"
restore() {
  make --no-print-directory security-deploy VARIANT= >/dev/null
}
trap restore EXIT

for id in $ids; do
  echo "=== $id"
  start=$(date +%s)
  variant=$id
  [ "$id" = baseline ] && variant=
  make --no-print-directory security-deploy VARIANT="$variant" >/dev/null
  # A variant's run exits 0 whatever it found; only a plan that did not run fails it.
  make --no-print-directory conformance >/dev/null 2>&1 || true
  kubectl -n security logs job/conformance-run | grep -E '^(MODULE|CONFORMANCE) ' | sed "s/^/$id /" >> "$out/$name.modules.log"
  line=$(kubectl -n security logs job/conformance-run | grep '^CONFORMANCE ' || true)
  if [ -z "$line" ]; then
    echo "no CONFORMANCE line for $id" >&2
    exit 1
  fi
  echo "$line" >> "$log"
  echo "$line" | python3 -c 'import json,sys; r=json.loads(sys.stdin.read().split(" ",1)[1]); print(" detected:", r["detected"], len(r["unexpected"]), r.get("unexpected_conditions"))'
  echo " $(( $(date +%s) - start )) s"
done

(cd services/security && uv run --locked python -m security_tools matrix "$OLDPWD/$log") > "$out/$name.md"
cat "$out/$name.md"
