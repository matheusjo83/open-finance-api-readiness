"""Operational job: the metrics of the operational requirements (pcm_metrics.py) over one run's PCM
reports of both sides, live (a validation run) or generated (a history), into Iceberg tables
lake.ofb.operational_*, partitioned by run (docs/operational-job.md).

With --check, the run must have labels (a history, jobs/history): every metric is compared with
them and the job fails on any difference. With --report NAME, the run's detection units
(detection.py) go to lake.ofb.operational_detection_units, and the detection report over
--report-runs (default: this run) to lake.ofb.operational_detection_* and
<source>/reports/NAME/operational.md (docs/operational-report.md); --gate fails on its invariants.

Usage: job.py --run-id <id> [--source s3a://lakehouse] [--requirements PATH] [--check]
              [--report NAME [--report-runs ID,...] [--gate]]
"""

from __future__ import annotations

import argparse
import json
import math
import sys

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from detection import gate, markdown, summarize, units
from pcm_metrics import LINE, build

REQUIREMENTS = "/opt/specs/operational/requirements.json"
TABLES = ["endpoint_day", "institution_day", "api_day", "endpoint_month", "institution_month", "api_month"]
# What the job must reproduce of each label scope: (label scope, our table, keys, columns).
COMPARED = [
    ("endpoint-day", "endpoint_day", ["endpoint", "httpMethod", "day"],
     ["calls", "unpaired_8h", "unpaired_7d", "inconsistent", "valid", "n529", "n_rt", "p95_ms", "minutes_available",
      "minutes_unavailable", "daily_availability", "rt_ok", "availability_ok"]),
    ("endpoint", "endpoint_day", ["endpoint", "httpMethod", "day"], ["long_availability", "long_ok"]),
    ("api-day", "api_day", ["api", "day"],
     ["calls", "unpaired_8h", "unpaired_7d", "inconsistent", "share_8h", "share_7d", "reporting_8h_ok", "reporting_7d_ok"]),
    ("institution-day", "institution_day", ["day"], ["valid", "n529", "share_529", "overload_ok", "overload_over_5"]),
]


def read_run(spark: SparkSession, source: str, run_id: str) -> tuple[DataFrame, DataFrame]:
    return tuple(spark.read.schema(LINE).json(f"{source}/pcm/{side}/run={run_id}/") for side in ("client", "server"))


def _same(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return a is not None and b is not None and math.isclose(a, b, rel_tol=1e-12, abs_tol=1e-12)
    return a == b


def compare(spark: SparkSession, tables: dict[str, DataFrame], source: str, run_id: str) -> list[str]:
    """Differences between the metrics and the run's labels, as readable lines."""
    problems = []
    for scope, table, keys, columns in COMPARED:
        labels = spark.read.json(f"{source}/pcm-labels/run={run_id}/{scope}/")
        if scope == "endpoint-day":
            labels = labels.withColumnRenamed("method", "httpMethod")
        if scope == "endpoint":
            labels = labels.withColumnRenamed("method", "httpMethod")
        ours = tables[table].withColumn("day", F.date_format("day", "yyyy-MM-dd"))
        expected = {tuple(r[k] for k in keys): r for r in labels.select(*keys, *columns).collect()}
        got = {tuple(r[k] for k in keys): r for r in ours.select(*keys, *columns).collect()}
        missing = sorted(set(expected) - set(got), key=str)
        problems += [f"{scope} {k}: no metrics" for k in missing[:5]]
        wrong = [(k, c, expected[k][c], got[k][c]) for k in expected if k in got for c in columns
                 if not _same(expected[k][c], got[k][c])]
        problems += [f"{scope} {k} {c}: label {e}, job {g}" for k, c, e, g in wrong[:10]]
        if len(wrong) > 10:
            problems.append(f"{scope}: {len(wrong)} differences in all")
    return problems


def main(argv: list[str] | None = None, spark: SparkSession | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source", default="s3a://lakehouse")
    parser.add_argument("--requirements", default=REQUIREMENTS)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--no-tables", action="store_true", help="compute and check only (tests)")
    parser.add_argument("--report", help="name of a detection report (the run must have labels)")
    parser.add_argument("--report-runs", help="comma-separated runs of the report (default: this run)")
    parser.add_argument("--gate", action="store_true", help="fail when the report's invariants break")
    args = parser.parse_args(argv)
    with open(args.requirements, encoding="utf-8") as f:
        req = json.load(f)

    own = spark is None
    if own:
        from lakehouse import session
        spark = session("operational")
        spark.sparkContext.setLogLevel("WARN")
    client, server = read_run(spark, args.source, args.run_id)
    tables = build(spark, client, server, req)

    if not args.no_tables:
        from lakehouse import CATALOG, replace_partitions
        namespace = f"{CATALOG}.ofb"
        spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {namespace}")
        for name in TABLES:
            replace_partitions(spark, f"{namespace}.operational_{name}", tables[name])

    pairing = {r["pairing"] or "LATE": r["count"] for r in tables["pairs"].groupBy("pairing").count().collect()}
    verdicts = {
        "endpoint_days_out_of_sla": tables["endpoint_day"].where(~F.col("rt_ok")).count(),
        "endpoint_days_unavailable": tables["endpoint_day"].where(~F.col("availability_ok")).count(),
        "institution_days_overloaded": tables["institution_day"].where(~F.col("overload_ok")).count(),
        "api_days_8h_missed": tables["api_day"].where(~F.col("reporting_8h_ok")).count(),
        "api_days_7d_missed": tables["api_day"].where(~F.col("reporting_7d_ok")).count(),
        "endpoint_months_rt_failed": tables["endpoint_month"].where(~F.col("rt_month_ok")).count(),
        "endpoint_months_availability_failed": tables["endpoint_month"].where(~F.col("availability_month_ok")).count(),
        "institution_months_overload_failed": tables["institution_month"].where(~F.col("overload_month_ok")).count(),
        "api_months_reporting_failed": tables["api_month"].where(~F.col("reporting_month_ok")).count(),
    }
    problems = compare(spark, tables, args.source, args.run_id) if args.check else []
    print("OPERATIONAL " + json.dumps({"run_id": args.run_id, "pairing": pairing, "verdicts": verdicts,
                                       "check": (problems or "ok") if args.check else "not asked"}), flush=True)
    for problem in problems:
        print(problem, file=sys.stderr)
    gate_problems = report(spark, args, tables) if args.report else []
    if own:
        spark.stop()
    return 1 if problems or (args.gate and gate_problems) else 0


def report(spark: SparkSession, args, tables: dict[str, DataFrame]) -> list[str]:
    """The detection report (detection.py) over --report-runs (default: this run), each with the
    detection units the job wrote for it; this run's units are written first."""
    labels = {scope: spark.read.json(f"{args.source}/pcm-labels/run={args.run_id}/{scope}/")
              for scope in ("endpoint-day", "api-day", "institution-day")}
    mine = units(tables, labels, args.run_id)
    runs = [r for r in (args.report_runs or args.run_id).split(",") if r]
    if args.no_tables:
        rows = [r.asDict() for r in mine.collect()] if runs == [args.run_id] else []
    else:
        from lakehouse import CATALOG, replace_partitions
        namespace = f"{CATALOG}.ofb"
        replace_partitions(spark, f"{namespace}.operational_detection_units", mine)
        rows = [r.asDict() for r in spark.table(f"{namespace}.operational_detection_units")
                .where(F.col("run_id").isin(runs)).collect()]
    missing = sorted(set(runs) - {r["run_id"] for r in rows})
    summary = summarize(rows)
    problems = [f"no detection units for {missing}: run the job over them first"] if missing else gate(summary)
    text = markdown(args.report, runs, summary, problems)
    if not args.no_tables:
        from lakehouse import CATALOG, replace_partitions
        for part, part_rows in summary.items():
            if part_rows:
                df = spark.createDataFrame([{**r, "report": args.report, "runs": ",".join(runs)} for r in part_rows])
                replace_partitions(spark, f"{CATALOG}.ofb.operational_detection_{part}", df, column="report")
        write_text(spark, f"{args.source}/reports/{args.report}/operational.md", text)
    print(text, flush=True)
    print("OPERATIONAL_REPORT " + json.dumps({"name": args.report, "runs": runs, "gate": problems or "ok",
                                              "verdicts": {r["verdict"]: [r["precision"], r["recall"]]
                                                           for r in summary["by_verdict"]}}), flush=True)
    return problems


def write_text(spark: SparkSession, uri: str, text: str) -> None:
    """One object through the Hadoop file system of the session (S3A in the cluster)."""
    jvm = spark.sparkContext._jvm
    path = jvm.org.apache.hadoop.fs.Path(uri)
    out = path.getFileSystem(spark.sparkContext._jsc.hadoopConfiguration()).create(path, True)
    try:
        out.write(bytearray(text.encode("utf-8")))
    finally:
        out.close()


if __name__ == "__main__":
    sys.exit(main())
