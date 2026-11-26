# Operational job

`jobs/operational` computes the metrics of the [operational requirements](operational.md) over
one run's PCM reports of both sides, live ([telemetry.md](telemetry.md)) or generated
([history.md](history.md)), into Iceberg tables. Every threshold is read from
`specs/operational/requirements.json`.

```bash
make operational                 # the proxy's current run (RUN_ID=... for another)
make operational-smoke           # the CI history, failing on any difference with its labels
```

## Pairing

The reports are paired as the PCM pairs them: by `fapiInteractionId`, then on `clientOrgId`,
`serverOrgId`, `statusCode`, `httpMethod` and `endpoint`. A side counts when it was sent within
7 days of the transaction, and for the reporting goal of 08:00, by 08:00 (Brasília) of the day
after the transaction's.

| Pairing (at 7 days) | Meaning |
|---|---|
| `PAIRED` | Both sides, the five fields equal |
| `PAIRED_INCONSISTENT` | Both sides, a field differs: left out of the response time, availability and overload |
| `UNPAIRED` | One side only |

The institution's view of an exchange is the client's report, or the server's when the client's
is missing (Manual de APIs 5.3.2 VI and VII, 5.4.2 IV and V).

## Tables

All in `lake.ofb`, partitioned by `run_id`.

| Table | One row per | Columns |
|---|---|---|
| `operational_endpoint_day` | endpoint, method and day | calls, pairing counts, `n_rt` and `p95_ms` against `sla_ms` (`rt_ok`); minutes available and unavailable, `daily_availability` (`availability_ok`); `long_availability`, the mean of the last 90 days' defined values (`long_ok`); `valid`, `n529` |
| `operational_institution_day` | server institution and day | `valid`, `n529`, `share_529`, `overload_ok` (below 0.5 %), `overload_over_5` |
| `operational_api_day` | API and day | `calls`, `unpaired_8h`, `unpaired_7d`, `inconsistent`; `share_8h` (at most 5 %) and `share_7d` (unpaired and inconsistent, at most 1 %) |
| `operational_endpoint_month` | endpoint, method and month | `rt_month_ok`: within the SLA on round(90 %) of the days with requests, the others at most 1.2 times it; `availability_month_ok`: every day within, and the long availability of the month's last day |
| `operational_institution_month` | institution and month | `overload_month_ok`: below 0.5 % on round(90 %) of the days, the others at most 5 % |
| `operational_api_month` | API and month | `reporting_month_ok`: the 08:00 goal missed on at most 3 days, the 7-day goal never |

A month is the days of it in the data. The CI history starts on 2026-08-18, so August has 14
days; the verdicts of a partial month are computed all the same.

The reporting shares count exchanges, not reports: an unpaired exchange has one report, an
inconsistent one two. The manual counts "reportes" against the "volume de chamadas"; with one
report per side the two readings agree on unpaired exchanges, which is what the goals limit.

## Checked against the history

With `--check`, every label of a history ([history.md](history.md)) must come out of the
metrics, value for value: per endpoint and day the counts, the P95, the minutes and the
verdicts; per endpoint the 90-day availability at the last day; per API and per day the
shares. The monthly verdicts follow from the days: the tests recompute them in plain Python from
the labels and compare.

The tests also run the job over four seeds at three volumes, and over the live runs the
validator's tests export (every exchange `PAIRED`). Hand-built cases pin what the history does
not reach: a P95 rank that rounds up (10 times: rank 10), a month of 31 days with 27 days within
(fails) and 28 (passes), a month with three late days (passes) and four (fails).

## Live runs

Over a validation run the job gives one day per endpoint. The defects of the proxy are contract
and business defects, so nothing fails there: every exchange is paired and the times are a few
milliseconds. The operational detection is measured on the history (item 18).
