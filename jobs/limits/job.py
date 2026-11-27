"""Limits job: judges one limits run from the client's log (judge.py) into Iceberg tables
lake.ofb.limits_{requests,cases}, partitioned by run (docs/limits-job.md).

With --report NAME, the run's detection units go to lake.ofb.limits_detection_units, and the
detection report over --report-runs (default: this run) to lake.ofb.limits_detection_* and
<source>/reports/NAME/limits.md; --gate fails on its invariants (precision and recall 1 for every
verdict measured, every edge as expected). The report is the operational one's (detection.py),
with the verdicts of judge.py.

Usage: job.py --run-id <id> [--source s3a://lakehouse] [--requirements PATH]
              [--report NAME [--report-runs ID,...] [--gate]] [--no-tables]
"""

from __future__ import annotations

import argparse
import json
import sys

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from detection import gate, markdown, summarize
from judge import CLIENT, VERDICTS, cases, judge, units

REQUIREMENTS = "/opt/specs/operational/requirements.json"


def main(argv: list[str] | None = None, spark: SparkSession | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source", default="s3a://lakehouse")
    parser.add_argument("--requirements", default=REQUIREMENTS)
    parser.add_argument("--no-tables", action="store_true", help="judge and report only (tests)")
    parser.add_argument("--report", help="name of a detection report (the run must have labels)")
    parser.add_argument("--report-runs", help="comma-separated runs of the report (default: this run)")
    parser.add_argument("--gate", action="store_true", help="fail when the report's invariants break")
    args = parser.parse_args(argv)
    with open(args.requirements, encoding="utf-8") as f:
        req = json.load(f)

    own = spark is None
    if own:
        from lakehouse import session
        spark = session("limits")
        spark.sparkContext.setLogLevel("WARN")
    client = spark.read.schema(CLIENT).json(f"{args.source}/limits/client/run={args.run_id}/")
    judged = judge(client, req).cache()
    per_case = cases(judged)

    if not args.no_tables:
        from lakehouse import CATALOG, replace_partitions
        namespace = f"{CATALOG}.ofb"
        spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {namespace}")
        replace_partitions(spark, f"{namespace}.limits_requests", judged)
        replace_partitions(spark, f"{namespace}.limits_cases", per_case)

    totals = judged.agg(F.count(F.lit(1)).alias("requests"),
                        F.sum(F.col("endpoint").isNull().cast("long")).alias("unmatched")).first()
    flagged = {r["verdict"]: [r["requests"], r["cases"]] for r in judged.where(F.col("verdict").isNotNull())
               .groupBy("verdict").agg(F.count(F.lit(1)).alias("requests"),
                                       F.countDistinct("origin", "endpoint").alias("cases")).collect()}
    print("LIMITS " + json.dumps({"run_id": args.run_id, "requests": totals["requests"],
                                  "unmatched": totals["unmatched"], "cases": per_case.count(),
                                  "flagged (requests, cases)": {v: flagged.get(v, [0, 0]) for v in VERDICTS}}),
          flush=True)
    problems = [f"{totals['unmatched']} requests match no endpoint of the requirements"] if totals["unmatched"] else []
    if not totals["requests"]:
        problems.append(f"no client log under limits/client/run={args.run_id}/")
    for problem in problems:
        print(problem, file=sys.stderr)
    gate_problems = report(spark, args, judged) if args.report and not problems else []
    if own:
        spark.stop()
    return 1 if problems or (args.gate and gate_problems) else 0


def report(spark: SparkSession, args, judged: DataFrame) -> list[str]:
    """The detection report over --report-runs (default: this run), each with the detection units
    the job wrote for it; this run's units are written first."""
    labels = spark.read.json(f"{args.source}/labels/run={args.run_id}/")
    mine = units(judged, labels)
    runs = [r for r in (args.report_runs or args.run_id).split(",") if r]
    if args.no_tables:
        rows = [r.asDict() for r in mine.collect()] if runs == [args.run_id] else []
    else:
        from lakehouse import CATALOG, replace_partitions
        namespace = f"{CATALOG}.ofb"
        replace_partitions(spark, f"{namespace}.limits_detection_units", mine)
        rows = [r.asDict() for r in spark.table(f"{namespace}.limits_detection_units")
                .where(F.col("run_id").isin(runs)).collect()]
    missing = sorted(set(runs) - {r["run_id"] for r in rows})
    summary = summarize(rows, VERDICTS)
    problems = [f"no detection units for {missing}: run the job over them first"] if missing else gate(summary)
    text = markdown(args.report, runs, summary, problems, VERDICTS, "Limits detection report", "docs/limits-job.md")
    if not args.no_tables:
        from lakehouse import CATALOG, replace_partitions
        for part, part_rows in summary.items():
            if part_rows:
                df = spark.createDataFrame([{**r, "report": args.report, "runs": ",".join(runs)} for r in part_rows])
                replace_partitions(spark, f"{CATALOG}.ofb.limits_detection_{part}", df, column="report")
        write_text(spark, f"{args.source}/reports/{args.report}/limits.md", text)
    print(text, flush=True)
    print("LIMITS_REPORT " + json.dumps({"name": args.report, "runs": runs, "gate": problems or "ok",
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
