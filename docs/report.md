# Detection report

`jobs/report` measures what each validation layer finds, against the ground truth the
[defect proxy](defects.md) writes. It is the last step of a run:

1. the proxy labels every exchange;
2. the [contract validator](validator.md) and the [business-rules job](business-rules.md) judge
   the exchanges without seeing the labels;
3. the report joins everything by `interaction_id`.

The latest reference campaign is in [detection-report.md](detection-report.md).

## What is measured

The unit is the **exchange**. A label makes an exchange positive for its defect class. The
proxy injects at most one defect per response. Each class belongs to one layer, and is measured
against that layer only:

| Layer | Flagged | Inconclusive |
|---|---|---|
| contract | verdict `fail` or `warn` | never |
| business | at least one `finding` | no finding, but at least one rule said `inconclusive` |

Per **defect class**:

- detected, inconclusive and missed (they add up to the positives);
- **strict recall**: detected / positives (inconclusive counts as missed), the cost of the whole
  pipeline;
- **conditional recall**: detected / (positives − inconclusive), the quality of the rules where
  they could decide.

Per **rule**, precision against the classes the rule is meant to find (`TARGETS` in
`metrics.py`). For example, `strict.undeclared-field` is measured on `undeclared-field`, and
`schema.body` on `missing-required-field` and `invalid-format`. Precision per class is not
defined, because one rule can serve two classes.

Per **layer**:

- precision: the flagged exchanges carrying a defect of that layer, over all flagged;
- recall: over all the layer's classes.

The **matrix** counts exchanges per label (rows) and per rule that fired (columns). It shows
overlaps and what one layer sees of the other's defects. For example, `missing-interaction-id`
also trips `schema.header`, but only on the Accounts 2xx responses.

Every proportion comes with a **95 % Wilson interval**. One run has 1 to 10 positives per class,
so a recall of 100 % over 2 positives means "between 34 % and 100 %". The interval says how
much a number can be trusted. Campaigns narrow it.

## Gate

The catalogue guarantees four invariants:

- precision 1 in both layers: nothing is flagged without its layer's defect;
- conditional recall 1 in every class;
- no inconclusive contract check;
- at least one detection.

`--gate` (`make report-smoke`, in CI) fails the job when one breaks. Strict recall of the
business layer is reported, not gated: it depends on how many inputs the contract layer
excluded, which varies with the seed and the day.

## Campaigns and reproducibility

```bash
make report                                   # the proxy's current run
make report RUNS=a,b REPORT_NAME=x            # several runs as one report
make campaign SEEDS=3 CUSTOMERS=50 REPORT_NAME=reference-YYYYMMDD
```

For each defect seed 1..SEEDS, `make campaign` (`scripts/campaign.sh`):

1. restarts the proxy with that seed and the run id `<name>-seed<k>`, and the transmitter with
   CUSTOMERS customers;
2. runs the validator and the business-rules job;
3. ends with one report over all the runs.

At the end it restores the defaults (seed 42, 5 customers). With 50 customers, a run has about
1,270 exchanges.

The numbers are reproducible for a given seed and day, not across days. The transmitter's
dataset is relative to the day it starts. The walk's booking window ends today, and that window
is in the query, so it enters the proxy's selection key (decision 17). The committed report is
therefore dated. Pinning "today" in the transmitter and the walk would make it exact, at some
cost in realism; it is left out for now.

## Output

- `lake.ofb.labels` (bronze): the proxy's labels, partitioned by `run_id`.
- `lake.ofb.detection_by_layer`, `detection_by_class`, `detection_by_rule`, `detection_matrix`
  (gold): the tables above, partitioned by `report` (the report name). Each also carries `runs`
  and `generated_at`, and a rerun replaces its report.
- `s3a://lakehouse/reports/<name>/report.md`: the Markdown report, also printed in the driver
  log.

## Tests

`make jobs-test` runs `jobs/tests/test_report.py` over the runs exported by the validator's
harness. It checks:

- the Wilson bounds;
- the invariants on the default-rate run;
- precision per rule and the matrix;
- that the gate catches an injected false positive and an injected miss;
- a report over two runs;
- the Markdown sections.
