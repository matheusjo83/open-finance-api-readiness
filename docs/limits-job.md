# Limits job

`jobs/limits` judges a [limits run](limits.md) from the client's log alone: the refusals the
client got, the requests it sent before them and the minimums of
`specs/operational/requirements.json`. The proxy's labels never decide a verdict; they make the
detection report's units (decision 47).

```bash
make limits-job                  # the limits proxy's current run (RUN_ID=... for another)
make limits-report               # the same, with the run's detection report gated (CI)
make limits-campaign SEEDS=5 LIMITS_REPORT=x   # a limits run per seed, one report over all
```

## Verdicts

Each request is matched to its endpoint of the requirements by method and path (a request that
matches none fails the job), which gives its TPM and monthly minimum, null where the limit does
not apply. The key is the request's origin (`x-mock-client-org-id`) and endpoint, as the
transmitter keys them.

| Verdict | A request is flagged when |
|---|---|
| `traffic_limit_early` | It got a 429 on an endpoint with a TPM, and the same origin sent that endpoint fewer than the TPM requests in the 60 seconds before it (`tpm_before`) |
| `operational_limit_early` | It got a 423 on an endpoint with a monthly minimum, and the same origin had fewer counted calls than the minimum for the same object in the same Brasília month before it (`counted_before`) |
| `limit_not_applicable` | It got a 429 or a 423 on an endpoint where neither limit applies (Consents) |

- **The 60 seconds** (divergence `tpm-window`): the manual does not say whether the minute is the
  calendar minute or any 60 seconds. Counting the 60 seconds before a 429 passes both a calendar
  and a sliding limiter that allow the minimum. The window includes its first millisecond, and
  requests that started in the same millisecond count in the order they ended.
- **Counted calls** (5.2, divergence `pagination-requests`): 2XX responses to a first page (no
  `page` parameter, or `page=1`). Pages after the first, refusals and errors do not count; a page
  after the first refused with 423 is judged like any other.
- **The month** is Brasília's (`America/Sao_Paulo`): 01:00 UTC on the 1st still belongs to the
  month before.
- **The object** is the client's `object` field (the consent for the list of accounts, the
  account otherwise); its customer follows from it, and the receiver is the origin.

A transmitter may let more through than the minimums, never fewer: a refusal that comes later
than the limit is not a defect, and nothing here flags it.

## Tables

All in `lake.ofb`.

| Table | Partition | One row per | Columns |
|---|---|---|---|
| `limits_requests` | `run_id` | request of the client's log | its fields, `endpoint`, `tpm`, `monthly`, `page_number`, `month`, `tpm_before`, `counted_before`, `verdict` |
| `limits_cases` | `run_id` | test case (origin and endpoint) | `requests`, and per verdict how many of its requests raised it |
| `limits_detection_units` | `run_id` | verdict and case it applies to | `causes`, `flagged` |
| `limits_detection_{by_verdict,by_cause,edges,matrix}` | `report` | as the [operational report](operational-report.md) | |

## Detection report

The unit is the test case: one origin on one endpoint. A verdict applies to the cases of the
endpoints it judges (TPM: Accounts; monthly: Accounts; not applicable: Consents). A case's causes
are the limit classes of its exchanges in the proxy's labels, as `class/variant`; a case with
none carries the edges the load builds:

- `tpm-at-limit`: a traffic case sends TPM + 50 requests in one calendar minute, so its 429 comes
  at the request after the TPM;
- `monthly-at-limit`: every Accounts case sends more counted calls than the monthly minimum, so
  its 423 comes at the success after the minimum.

Both must not be flagged; `make limits-smoke` checks that the transmitter put those refusals
exactly there. A case whose class changed nothing (a `counts-*` draw with traffic that neither
paginates nor fails before the limit) has no labelled exchange and counts as clean.

| Verdict | Targets (recall) | Also allowed (precision) | Edges |
|---|---|---|---|
| `traffic_limit_early` | `early-traffic-limit` | | `tpm-at-limit`: not flagged |
| `operational_limit_early` | `early-operational-limit` | `early-traffic-limit` | `monthly-at-limit`: not flagged |
| `limit_not_applicable` | `limit-not-applicable` | | |

`early-traffic-limit` may explain an early 423: the proxy turned into 429 a success the
transmitter counted, so the client sees the transmitter's 423 after fewer successes than the
minimum. The report and its gate are the operational report's (`operational/detection.py`, with
this job's verdicts): precision and recall 1 for every verdict measured, every edge as expected,
95 % Wilson intervals. CI runs one run with seed 42, which measures the three verdicts: 2
`traffic_limit_early`, 5 `operational_limit_early` and 1 `limit_not_applicable` positives among
34 cases.

## Tests

`jobs/tests/test_limits.py` builds client logs by hand: each verdict at its limit and one
request before it, a sliding minute across a calendar boundary, requests older than 60 seconds,
ties in one millisecond, pages, failures and another object not counted, the Brasília month,
Consents, the endpoint of a path with a query, and the gate on a missed flag, a false flag and an
edge. Changing any of these rules in `judge.py` (`<=` for `<`, every page or every status counted,
the UTC month, a wider window, the order of ties) fails a test.

## Trial campaign

`make limits-campaign SEEDS=5` on 2026-11-27 (kind on a laptop, about 2.5 minutes a seed): 170
cases, 92,885 requests, every variant of the three classes drawn at least twice.

| Verdict | Cases | Flagged | Positives | Precision | Recall |
|---|---|---|---|---|---|
| `traffic_limit_early` | 120 | 12 | 12 | 100 % [76–100] | 100 % [76–100] |
| `operational_limit_early` | 120 | 31 | 31 | 100 % [89–100] | 100 % [89–100] |
| `limit_not_applicable` | 50 | 10 | 10 | 100 % [72–100] | 100 % [72–100] |

The edges held in every case built at them: 16 at `tpm-at-limit`, 77 at `monthly-at-limit`. No
flagged case in these seeds needed the allowance of `early-traffic-limit` for an early 423.
