"""Detection report job: joins one or more validation runs with the proxy's labels and measures
each layer (metrics.py, docs/report.md).

Reads the labels from s3a://lakehouse/labels/run=<id>/ (and keeps them as the bronze table
lake.ofb.labels), the exchanges, contract results and business checks from Iceberg. Writes
lake.ofb.detection_{by_layer,by_class,by_rule,matrix}, partitioned by report name, and the
Markdown report to <source>/reports/<name>/report.md. Prints both.

Usage: job.py --runs <id>[,<id>...] [--name <report>] [--source s3a://lakehouse] [--gate]
--gate fails the job when an invariant breaks (precision 1 in both layers, conditional recall
1 in every class).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone

from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql import types as T

from lakehouse import CATALOG, replace_partitions, session
from metrics import aggregates, compute, gate, markdown, per_exchange

NAMESPACE = f"{CATALOG}.ofb"
S = T.StringType()
LABEL_SCHEMA = T.StructType([
    T.StructField("run_id", S), T.StructField("ts", S), T.StructField("interaction_id", S),
    T.StructField("method", S), T.StructField("path", S), T.StructField("route", S), T.StructField("query", S),
    T.StructField("consent", S), T.StructField("upstream_status", T.IntegerType()),
    T.StructField("status", T.IntegerType()), T.StructField("seed", T.LongType()),
    T.StructField("defect_class", S), T.StructField("defect_layer", S), T.StructField("defect_variant", S),
    T.StructField("defect_target", S), T.StructField("defect_detail", S),
])


def write_text(spark: SparkSession, uri: str, text: str) -> None:
    """One object through the Hadoop file system of the session (S3A in the cluster)."""
    jvm = spark.sparkContext._jvm
    path = jvm.org.apache.hadoop.fs.Path(uri)
    fs = path.getFileSystem(spark.sparkContext._jsc.hadoopConfiguration())
    out = fs.create(path, True)
    try:
        out.write(bytearray(text.encode("utf-8")))
    finally:
        out.close()


TEXT_COLUMNS = {"defect_class", "layer", "rule", "targets", "label", "report", "runs"}
RATIO_PREFIXES = ("precision", "recall")


def _type(column: str) -> T.DataType:
    if column in TEXT_COLUMNS:
        return T.StringType()
    return T.DoubleType() if column.startswith(RATIO_PREFIXES) else T.LongType()


def table_rows(spark: SparkSession, rows: list[dict], name: str, runs: str, generated_at: datetime,
               columns: list[str] | None = None):
    """A metrics table: its rows plus the report name, the runs and when it was made. The schema
    is explicit (a ratio without observations is null in every row)."""
    rows = [{**r, "report": name, "runs": runs} for r in rows]
    names = list(rows[0]) if rows else [*(columns or []), "report", "runs"]
    schema = T.StructType([T.StructField(c, _type(c)) for c in names])
    df = spark.createDataFrame([tuple(r[c] for c in schema.names) for r in rows], schema)
    return df.withColumn("generated_at", F.lit(generated_at).cast("timestamp"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", required=True, help="comma-separated run ids")
    parser.add_argument("--name", help="report name (default: the first run id)")
    parser.add_argument("--source", default="s3a://lakehouse")
    parser.add_argument("--gate", action="store_true")
    args = parser.parse_args()
    runs = [r for r in args.runs.split(",") if r]
    name = args.name or runs[0]

    spark = session("detection-report")
    spark.sparkContext.setLogLevel("WARN")
    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {NAMESPACE}")
    labels = (spark.read.schema(LABEL_SCHEMA).json([f"{args.source}/labels/run={r}/" for r in runs])
              .withColumn("ts", F.to_timestamp("ts")))
    replace_partitions(spark, f"{NAMESPACE}.labels", labels)

    def table(t: str):
        return spark.table(f"{NAMESPACE}.{t}").where(F.col("run_id").isin(runs))

    missing = [r for r in runs if table("business_checks").where(F.col("run_id") == r).limit(1).count() == 0]
    if missing:
        print(f"no business checks for {missing}: run make business-rules first", file=sys.stderr)
        return 2

    rows = per_exchange(table("exchanges"), table("contract_results"), table("business_checks"), table("labels"))
    agg = aggregates(rows)
    metrics = compute(agg)
    problems = gate(metrics)
    report = markdown(name, agg, metrics, problems)

    generated_at = datetime.now(timezone.utc).replace(tzinfo=None)
    for part, rows_ in (("by_layer", metrics["by_layer"]), ("by_class", metrics["by_class"]),
                        ("by_rule", metrics["by_rule"]), ("matrix", metrics["matrix"])):
        replace_partitions(spark, f"{NAMESPACE}.detection_{part}",
                           table_rows(spark, rows_, name, ",".join(runs), generated_at,
                                      columns=["label", "rule", "exchanges"]), column="report")
    uri = f"{args.source}/reports/{name}/report.md"
    write_text(spark, uri, report)

    print(report, flush=True)
    print("REPORT " + json.dumps({"name": name, "runs": runs, "exchanges": agg["exchanges"], "markdown": uri,
                                  "layers": {r["layer"]: {"precision": r["precision"], "recall_strict": r["recall_strict"],
                                                          "recall_conditional": r["recall_conditional"]}
                                             for r in metrics["by_layer"]},
                                  "gate": problems or "ok"}), flush=True)
    spark.stop()
    return 1 if args.gate and problems else 0


if __name__ == "__main__":
    sys.exit(main())
