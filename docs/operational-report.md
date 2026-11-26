# Operational detection report

What each verdict of the [operational job](operational-job.md) flags, against the episodes the
[history](history.md) built. The detection report of the other layers ([report.md](report.md))
works per exchange; this one works per the verdict's own unit, and is kept apart.

```bash
make operational-smoke                          # the CI history, its report gated
make operational-campaign SEEDS=5 OPERATIONAL_REPORT=x   # a history per seed, one report over all
```

The job writes, per run, one row per verdict and unit to `lake.ofb.operational_detection_units`;
the report aggregates the runs it is given into `lake.ofb.operational_detection_{by_verdict,
by_cause,edges,matrix}` (partitioned by report name) and `s3a://lakehouse/reports/<name>/operational.md`.

## How it is measured

A unit carries the causes the history put in it: the class (and variant) or edge of each of its
days, from the labels. Nothing is recomputed: the flags come from the job's tables.

| Verdict | Unit | Targets (recall) | Also allowed (precision) | Edges |
|---|---|---|---|---|
| `rt_day` | endpoint-day | `slow-day` | | `p95-at-sla`: not flagged |
| `availability_day` | endpoint-day | `outage/daily` | `overload`, `overload-at-0.5` (a 529 is an error) | `minute-at-95`, `day-at-95`: not flagged |
| `long_availability` | endpoint, at the last day | `outage-chronic` | `outage`, `overload`, `overload-at-0.5` | |
| `overload_day` | institution-day | `overload` | | `overload-at-0.5`: flagged |
| `reporting_8h_day` | API-day | `unpaired` `late-*`, `missing-many-*` | `unpaired` | |
| `reporting_7d_day` | API-day | `unpaired` `missing-*`, `inconsistent-pair` | | |
| `rt_month` | endpoint-month | `slow-day/severe` | `slow-day` | |
| `availability_month` | endpoint-month | `outage/daily` | `outage-chronic`, `overload`, `overload-at-0.5`, and the causes of the 90-day window of the month's last day | |
| `overload_month` | institution-month | `overload/over-5` | `overload` | |
| `reporting_month` | API-month | `unpaired` `missing-*`, `inconsistent-pair` | `unpaired` (late more than three days) | |

- **Precision**: flags whose unit has a target or allowed cause, over the flags.
- **Recall**: units with a target cause that were flagged, over those units.
- **Edges**: units built exactly at a threshold must come out as the requirements say.
- Intervals: 95 % Wilson. The gate wants precision and recall 1 for every verdict measured,
  and every edge as expected. A verdict with no positive unit in the runs is reported as not
  measured; it does not break the gate. The CI seed measures all of them (a test checks it).

The allowed causes are what the requirements imply, not tolerance: a month's availability takes
the long availability of its last day, whose window reaches back into earlier months, so an
earlier outage may fail it; a 529 is a 5XX, so an overload may fail a day's availability
(`overload-in-availability`). They may explain a flag; only the targets must cause one.

## What building the report found

- The history spread each 529 of an overload day over its own minute. At 3 requests per minute
  each one took its minute's availability with it, so one `over-5` day failed the day of every
  endpoint and sank all their 90-day means: the long availability flagged 11 endpoints of 11 and
  measured nothing. An overload is a burst over the TPS limit, so the history now puts a unit's
  529s in one minute; the long availability flags 4 to 6 endpoints per seed, the chronic ones
  among them.
- The first version took a month's causes from its own days only, so a month failed by an
  earlier outage in its 90-day window counted as a false flag (precision 90 %), and once the
  window counted as a cause, recall fell to 74 %. The window is now context: it may explain a
  flag, and only the month's own days make it a positive.

## Reference campaign

`make operational-campaign SEEDS=5 OPERATIONAL_REPORT=operational-reference`, 2026-11-26, at the
CI volume (30 minutes x 3 requests per endpoint and day, 90 days, 11 endpoints).

Five histories (seeds 1 to 5), each checked against its labels first: 4,950 endpoint-days and
464,198 exchanges in all. Every verdict holds its gate: precision and recall
100 %, and the 143 units at a threshold come out as the requirements say. What the figures add
to the gate:

- `availability_day` flags exactly the 126 outage days: with the 529s in one burst minute, no
  overload day fails a day's availability any more (the literal reading would still allow it).
- `long_availability` flags 24 of 55 endpoint windows: the 16 chronic ones, and 8 whose outage
  and overload days add up to more than 0.5 % over 90 days. The requirements allow both.
- `availability_month` flags 131 of 220 months, 92 of them for an outage day of their own; the
  others have a chronic outage, an overload, or an earlier outage in the 90-day window of their
  last day.
  Months are partial at the ends of the window (14 days of August).
- In the matrix a unit counts once per cause, and a month or an endpoint window has many, so
  its rows overlap.

Runs: `history-s1-90d-30x3`, `history-s2-90d-30x3`, `history-s3-90d-30x3`, `history-s4-90d-30x3`, `history-s5-90d-30x3`. Intervals: 95 % Wilson. A flag is right when its unit has an allowed cause; recall is over the units with a target cause (docs/operational-report.md).

### By verdict

| Verdict | Unit | Units | Flagged | Precision | Positives | Recall |
|---|---|---|---|---|---|---|
| `rt_day` | endpoint-day | 4950 | 181 | 100.0 % [98–100] | 181 | 100.0 % [98–100] |
| `availability_day` | endpoint-day | 4950 | 126 | 100.0 % [97–100] | 126 | 100.0 % [97–100] |
| `long_availability` | endpoint | 55 | 24 | 100.0 % [86–100] | 16 | 100.0 % [81–100] |
| `overload_day` | institution-day | 450 | 30 | 100.0 % [89–100] | 17 | 100.0 % [82–100] |
| `reporting_8h_day` | api-day | 900 | 47 | 100.0 % [92–100] | 47 | 100.0 % [92–100] |
| `reporting_7d_day` | api-day | 900 | 53 | 100.0 % [93–100] | 53 | 100.0 % [93–100] |
| `rt_month` | endpoint-month | 220 | 75 | 100.0 % [95–100] | 72 | 100.0 % [95–100] |
| `availability_month` | endpoint-month | 220 | 131 | 100.0 % [97–100] | 92 | 100.0 % [96–100] |
| `overload_month` | institution-month | 20 | 8 | 100.0 % [68–100] | 8 | 100.0 % [68–100] |
| `reporting_month` | api-month | 40 | 26 | 100.0 % [87–100] | 26 | 100.0 % [87–100] |

### By cause

| Verdict | Cause | Units | Flagged | Recall |
|---|---|---|---|---|
| `rt_day` | `slow-day/mild` | 95 | 95 | 100.0 % [96–100] |
| `rt_day` | `slow-day/severe` | 86 | 86 | 100.0 % [96–100] |
| `availability_day` | `outage/daily` | 126 | 126 | 100.0 % [97–100] |
| `long_availability` | `outage-chronic` | 16 | 16 | 100.0 % [81–100] |
| `overload_day` | `overload/over-0.5` | 8 | 8 | 100.0 % [68–100] |
| `overload_day` | `overload/over-5` | 9 | 9 | 100.0 % [70–100] |
| `reporting_8h_day` | `unpaired/late-client` | 9 | 9 | 100.0 % [70–100] |
| `reporting_8h_day` | `unpaired/late-server` | 15 | 15 | 100.0 % [80–100] |
| `reporting_8h_day` | `unpaired/missing-many-client` | 13 | 13 | 100.0 % [77–100] |
| `reporting_8h_day` | `unpaired/missing-many-server` | 10 | 10 | 100.0 % [72–100] |
| `reporting_7d_day` | `inconsistent-pair/endpoint` | 10 | 10 | 100.0 % [72–100] |
| `reporting_7d_day` | `inconsistent-pair/status` | 6 | 6 | 100.0 % [61–100] |
| `reporting_7d_day` | `unpaired/missing-many-client` | 13 | 13 | 100.0 % [77–100] |
| `reporting_7d_day` | `unpaired/missing-many-server` | 10 | 10 | 100.0 % [72–100] |
| `reporting_7d_day` | `unpaired/missing-some-client` | 7 | 7 | 100.0 % [65–100] |
| `reporting_7d_day` | `unpaired/missing-some-server` | 7 | 7 | 100.0 % [65–100] |
| `rt_month` | `slow-day/severe` | 72 | 72 | 100.0 % [95–100] |
| `availability_month` | `outage/daily` | 92 | 92 | 100.0 % [96–100] |
| `overload_month` | `overload/over-5` | 8 | 8 | 100.0 % [68–100] |
| `reporting_month` | `inconsistent-pair/endpoint` | 9 | 9 | 100.0 % [70–100] |
| `reporting_month` | `inconsistent-pair/status` | 5 | 5 | 100.0 % [57–100] |
| `reporting_month` | `unpaired/missing-many-client` | 9 | 9 | 100.0 % [70–100] |
| `reporting_month` | `unpaired/missing-many-server` | 9 | 9 | 100.0 % [70–100] |
| `reporting_month` | `unpaired/missing-some-client` | 6 | 6 | 100.0 % [61–100] |
| `reporting_month` | `unpaired/missing-some-server` | 6 | 6 | 100.0 % [61–100] |

### Edges

Units built exactly at a threshold.

| Verdict | Edge | Expected | Units | As expected |
|---|---|---|---|---|
| `rt_day` | `p95-at-sla` | not flagged | 48 | 48 |
| `availability_day` | `minute-at-95` | not flagged | 42 | 42 |
| `availability_day` | `day-at-95` | not flagged | 40 | 40 |
| `overload_day` | `overload-at-0.5` | flagged | 13 | 13 |

### What flagged what

Flagged units per cause (rows) and verdict (columns); a unit with several causes counts in each.

| Cause | `rt_day` | `availability_day` | `long_availability` | `overload_day` | `reporting_8h_day` | `reporting_7d_day` | `rt_month` | `availability_month` | `overload_month` | `reporting_month` |
|---|---|---|---|---|---|---|---|---|---|---|
| `edge:day-at-95` |  |  | 15 |  |  |  | 16 | 24 |  |  |
| `edge:minute-at-95` |  |  | 12 |  |  |  | 13 | 20 |  |  |
| `edge:overload-at-0.5` |  |  | 20 | 13 |  |  | 34 | 50 | 5 |  |
| `edge:p95-at-sla` |  |  | 18 |  |  |  | 17 | 29 |  |  |
| `inconsistent-pair/endpoint` |  |  | 15 |  |  | 10 | 15 | 31 |  | 9 |
| `inconsistent-pair/status` |  |  | 14 |  |  | 6 | 13 | 18 |  | 5 |
| `outage-chronic` |  |  | 16 |  |  |  | 16 | 61 |  |  |
| `outage/daily` |  | 126 | 23 |  |  |  | 39 | 92 |  |  |
| `overload/over-0.5` |  |  | 20 | 8 |  |  | 23 | 56 | 2 |  |
| `overload/over-5` |  |  | 18 | 9 |  |  | 32 | 53 | 8 |  |
| `slow-day/mild` | 95 |  | 18 |  |  |  | 28 | 47 |  |  |
| `slow-day/severe` | 86 |  | 19 |  |  |  | 72 | 46 |  |  |
| `unpaired/late-client` |  |  | 13 |  | 9 |  | 16 | 29 |  | 2 |
| `unpaired/late-server` |  |  | 21 |  | 15 |  | 26 | 44 |  | 9 |
| `unpaired/missing-many-client` |  |  | 20 |  | 13 | 13 | 22 | 30 |  | 9 |
| `unpaired/missing-many-server` |  |  | 16 |  | 10 | 10 | 19 | 30 |  | 9 |
| `unpaired/missing-some-client` |  |  | 12 |  |  | 7 | 12 | 25 |  | 6 |
| `unpaired/missing-some-server` |  |  | 12 |  |  | 7 | 8 | 22 |  | 6 |

### Gate

All invariants hold: precision and recall 1 for every verdict, every edge as expected.
