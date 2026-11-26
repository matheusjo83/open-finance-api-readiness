# History

The operational requirements judge minutes, days, months and a 90-day moving average
([operational.md](operational.md)); a validation run lasts minutes. `jobs/history` generates the
rest: 90 days of PCM reports of both sides, in the format of the live telemetry
([telemetry.md](telemetry.md)), with operational defects built in and labelled.

```bash
make history                                                # CI's volume: 30 minutes x 3 requests per endpoint and day
make history HISTORY_SEED=2 HISTORY_MINUTES=1440 HISTORY_PER_MINUTE=20   # every minute
```

Output, under `run=history-s<seed>-90d-<minutes>x<per minute>`:

| Object | What |
|---|---|
| `s3://lakehouse/pcm/client/run=<id>/`, `pcm/server/run=<id>/` | The reports, one JSON line each, as the validator and the proxy write them |
| `s3://lakehouse/pcm-labels/run=<id>/endpoint-day/` | Per endpoint and day: the episode, the ledger and the verdicts it implies |
| `.../endpoint/` | Per endpoint: the 90-day availability at the last day, chronic or not |
| `.../api-day/` | Per API and day: reports unpaired by 08:00 of the next day and within 7 days |
| `.../institution-day/` | Per day: the share of 529 |

## How a unit is built

The unit is an endpoint on a day (Brasília time). It is generated from the seed, the endpoint
and the day alone, so Spark spreads the units over its executors and a day comes out the same
whatever else is generated. The traffic: `minutes` minutes of the day, `per minute` requests
each, started up to 59.99 seconds into the minute (the server's report, 2 ms later, stays in the
same minute and day). Reports go within half an hour, 5 % of them in the last hour before 08:00
of the next day. Noise, away from the edges and never on a minute's first call: 2 % of 404 (not a valid
request), 2 % of 422 (a success) and 1 % of reports with time 0 (left out of the response time).

A unit gets at most one episode, from the widest scope that has one: the day of the institution,
then the day of the API, then the endpoint's own. The draw is a hash of the seed and the scope's
key against the rates, as the proxy's (decision 17).

| Class | Scope | Variants | What it does | Expected |
|---|---|---|---|---|
| `slow-day` | endpoint-day | `mild`, `severe` | The time at the P95 rank above the SLA: up to 1.2 times, or more | Day out of the SLA; `severe` fails the month on its own |
| `outage` | endpoint-day | `daily` | Every request of a few minutes fails (5XX, 408) | Day below 95 % |
| `outage-chronic` | endpoint | | 15 days with one or more failed minutes each, every day at 95 % or more | 90-day mean below 99.5 % |
| `overload` | institution-day | `over-0.5`, `over-5` | 529s: above 0.5 % and at most 5 % of the valid requests, or above 5 %, in one burst minute per endpoint | Day over the limit; `over-5` fails the month on its own |
| `unpaired` | API-day | `late-*`, `missing-some-*`, `missing-many-*` (`client` or `server`) | One side reports after 08:00 of the next day (>5 %), or never (1 to 5 %, or >5 %) | `late`: fails at 08:00 only; `missing-some`: at 7 days only; `missing-many`: both |
| `inconsistent-pair` | API-day | `status`, `endpoint` | The server reports another status or endpoint (>1 %) | Fails at 7 days |

The edges are days at a threshold, with the verdict the requirements give there:

| Edge | Scope | Built | Verdict |
|---|---|---|---|
| `p95-at-sla` | endpoint-day | The time at the P95 rank is the SLA | Within (at most, `p95-boundary`) |
| `minute-at-95` | endpoint-day | One minute with 19 successes and 1 error | Available |
| `day-at-95` | endpoint-day | 95 % of the minutes available | Within |
| `overload-at-0.5` | institution-day | 529s exactly 0.5 % of the valid requests | Over the limit (it must be below 0.5 %) |

The times are built around the P95: the counted calls get times at most the target below its
rank, the target at the rank, more above. Calls the response time leaves out (529,
`PAIRED_INCONSISTENT`, time 0) get 14 seconds, so a metric that counted them would show it.

## Ledger and labels

Each unit keeps a ledger of what it built: calls, valid requests and 529s, minutes available and
unavailable, the counted calls and the time at the P95 rank, calls unpaired at 08:00 and at 7
days, calls inconsistent. The unit checks the ledger against its episode and raises when an
episode missed its threshold or a clean unit crossed one. The labels are these ledgers and their
sums per API and per day, with the verdicts of the requirements.

The ledger counts what was built, not what the reports say. The tests recount every unit of
three seeds from its reports alone, pairing them as the PCM does, and must find the ledger; the
operational job (item 16) must find it too.

What the labels do not decide:

- A 529 is a 5XX, so it is an error in availability (`overload-in-availability`): an overload
  day may also fail the day's availability, and the label says so. The 529s of a unit come in one
  minute, as a burst over the TPS limit would: spread one per minute, they took every endpoint's
  90 days down with one overload day ([operational-report.md](operational-report.md)).
- The monthly verdicts follow from the daily ones; item 16 computes them for both.

## Volume

| Settings | Reports | Job in the local cluster |
|---|---|---|
| 30 minutes x 3 (CI) | about 181,000 (94 MB of JSON) | 43 s |
| 1440 minutes x 20 | about 57 million (some 30 GB) | not run: the scale benchmark of the backlog |

The generator itself takes about 2 ms per unit at CI's volume and 0.6 s per unit at every minute.
