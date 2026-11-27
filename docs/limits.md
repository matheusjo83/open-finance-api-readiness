# Limits

How the transmitter's [limits](transmitter.md#limits) are driven and judged. The minimums come
from [operational.md](operational.md); a transmitter may let more through, never fewer, so the
defects are refusals that come too early or where no limit applies
([limit classes](defects.md#limit-classes)). Decision 45 has the design.

```bash
make limits LIMITS_SEED=42   # one run: limits proxy with that seed, fresh transmitter, k6, upload
make limits-smoke            # checks the last run
```

## A run

`scripts/limits-run.sh` gives the limits proxy (`proxy-limits`) the seed and a run id of its own
(`limits-s<seed>-<UTC time>`) and restarts the transmitter, so both start from zero counters: the
load's origins are the same in every run, and the proxy's draw depends on the seed only. It waits
until both Services answer from the new pods (the proxy's run id, the transmitter's instance in
`/healthz`), then runs the Job `limits-k6` (`infra/k8s/limits-k6.yaml`):

1. k6 2.3.0 (`grafana/k6` by digest, pulled from Docker Hub: kind cannot load the multi-platform
   image from the local store) runs `infra/k6/limits.js` against `http://proxy-limits.ofb.svc`;
2. the validator's image uploads k6's log (`python -m contract_validator.limits_log`) to
   `s3://lakehouse/limits/client/run=<run_id>/` and flushes the proxy's labels of the same run.

Each request is one record of the client's view, written by the script with `console.log` into
k6's JSON log rather than k6's metrics output (about 14 points per request):

| Field | |
|---|---|
| `run_id` | The limits proxy's run |
| `case`, `kind`, `route` | The test case, its kind (`traffic`, `operational`, `consents`) and the proxy's route name |
| `origin` | The `x-mock-client-org-id` sent: a UUID from the SHA-256 of `limits-case-<case>` |
| `object` | The consent (list of accounts) or the account the monthly limit counts |
| `page` | 1, or 2 for the page after the first |
| `method`, `path`, `interaction`, `status` | The request, its `x-fapi-interaction-id` and the status the client got |
| `start`, `end` | Epoch milliseconds around the call |

## Test cases

One origin per case, so each case is a key of its own at the transmitter and a draw of its own at
the proxy.

| Kind | Cases | Requests |
|---|---|---|
| `traffic` | one per Accounts endpoint | From the next minute boundary, TPM + 50 in a row, the same first page: the one after the TPM must be a 429, none before (the monthly limit answers 423 long before, which does not change the count) |
| `operational` | three per Accounts endpoint | Monthly minimum + 3 rounds of: the first page, page 2 where the endpoint paginates (list, transactions), a request that fails (an unknown account or an invalid parameter); the success after the minimum must be a 423, none before |
| `consents` | two per Consents endpoint | 25 requests (create, read, history; delete and renew of ids that answer 404 and 401): never 429 nor 423 |

About 18,600 requests per run. The traffic cases start together after the minute boundary, so the
run lasts about a minute; on a laptop the bursts take 8 to 20 seconds, and the smoke check fails
if one does not fit in its minute.

## Checks

`make limits-smoke` (`scripts/limits_smoke.py`, in the limits proxy's pod) reads the client's log
and the labels of the last run and checks that:

- every request has its label, with the status the client got;
- each traffic burst stayed in one calendar minute;
- the clean cases (no exchange with a limit class) met the transmitter's limits exactly at the
  minimums: 429 at TPM + 1 and none before, 423 at the success after the minimum and none before,
  never on Consents;
- the run has at least one limit defect.

It prints the cases, the clean ones per kind, the defects per class and variant, and how long
each burst took. With seed 42 the same eight cases carry a defect in every run.
`services/validator/tests/test_limits_log.py` checks the upload.
