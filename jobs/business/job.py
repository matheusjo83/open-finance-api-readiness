"""Business-rules job: reads one validation run (the validator's exchange log and contract
results in s3a://lakehouse/), builds the bronze, silver and gold tables (rules.py) and writes
them to Iceberg (lake.ofb.*), replacing the run's partition. Prints a summary line.

Usage: job.py --run-id <id> [--source s3a://lakehouse] [--verify-labels]
--verify-labels then compares the checks with the proxy's labels (verify.py) and fails the job
if they disagree: a test harness for make business-smoke.
"""

from __future__ import annotations

import argparse
import json
import sys

from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

from lakehouse import CATALOG, session
from rules import CONTRACT_SCHEMA, EXCHANGE_SCHEMA, build
from verify import compare

NAMESPACE = f"{CATALOG}.ofb"
# Write order: bronze, silver, gold.
TABLES = ["exchanges", "contract_results", "consent_reads", "balances", "listing_pages", "listing_items",
          "business_checks"]


def write(spark: SparkSession, name: str, df: DataFrame) -> None:
    """Replaces the run's partition (run_id), creating the table on the first run."""
    table = f"{NAMESPACE}.{name}"
    if spark.catalog.tableExists(table):
        df.writeTo(table).overwritePartitions()
    else:
        df.writeTo(table).partitionedBy(F.col("run_id")).create()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source", default="s3a://lakehouse")
    parser.add_argument("--verify-labels", action="store_true")
    args = parser.parse_args()

    spark = session("business-rules")
    spark.sparkContext.setLogLevel("WARN")  # the summary lines stay in the tail of the driver log
    run = f"run={args.run_id}"
    exchanges = spark.read.schema(EXCHANGE_SCHEMA).json(f"{args.source}/exchanges/{run}/")
    contract = spark.read.schema(CONTRACT_SCHEMA).json(f"{args.source}/contract/{run}/")
    tables = build(exchanges, contract)

    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {NAMESPACE}")
    for name in TABLES:
        write(spark, name, tables[name])

    checks = spark.table(f"{NAMESPACE}.business_checks").where(F.col("run_id") == args.run_id)
    outcomes = {f"{r['rule']}:{r['outcome']}": r["count"]
                for r in checks.groupBy("rule", "outcome").count().orderBy("rule", "outcome").collect()}
    counts = {name: spark.table(f"{NAMESPACE}.{name}").where(F.col("run_id") == args.run_id).count() for name in TABLES}
    print("BUSINESS " + json.dumps({"run_id": args.run_id, "rows": counts, "checks": outcomes}), flush=True)

    status = 0
    if args.verify_labels:
        labels = spark.read.json(f"{args.source}/labels/{run}/")
        by_class, wrong = compare(spark.table(f"{NAMESPACE}.exchanges").where(F.col("run_id") == args.run_id),
                                  checks, labels)
        print("VERIFY " + json.dumps({"by_class": by_class, "disagreements": wrong}), flush=True)
        if wrong or not any(v.get("flagged") for v in by_class.values()):
            status = 1
    spark.stop()
    return status


if __name__ == "__main__":
    sys.exit(main())
