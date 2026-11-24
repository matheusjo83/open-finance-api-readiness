"""Business-rules job: reads one validation run (the validator's exchange log and contract
results in s3a://lakehouse/), builds the bronze, silver and gold tables (rules.py) and writes
them to Iceberg (lake.ofb.*), replacing the run's partition. Prints a summary line.

Usage: job.py --run-id <id> [--source s3a://lakehouse]
The comparison with the proxy's labels is the detection report's (jobs/report).
"""

from __future__ import annotations

import argparse
import json
import sys

from pyspark.sql import functions as F

from lakehouse import CATALOG, replace_partitions, session
from rules import CONTRACT_SCHEMA, EXCHANGE_SCHEMA, build

NAMESPACE = f"{CATALOG}.ofb"
# Write order: bronze, silver, gold.
TABLES = ["exchanges", "contract_results", "consent_reads", "balances", "listing_pages", "listing_items",
          "extension_items", "renewals",
          "business_checks"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--source", default="s3a://lakehouse")
    args = parser.parse_args()

    spark = session("business-rules")
    spark.sparkContext.setLogLevel("WARN")  # the summary lines stay in the tail of the driver log
    run = f"run={args.run_id}"
    exchanges = spark.read.schema(EXCHANGE_SCHEMA).json(f"{args.source}/exchanges/{run}/")
    contract = spark.read.schema(CONTRACT_SCHEMA).json(f"{args.source}/contract/{run}/")
    tables = build(exchanges, contract)

    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {NAMESPACE}")
    for name in TABLES:
        replace_partitions(spark, f"{NAMESPACE}.{name}", tables[name])

    checks = spark.table(f"{NAMESPACE}.business_checks").where(F.col("run_id") == args.run_id)
    outcomes = {f"{r['rule']}:{r['outcome']}": r["count"]
                for r in checks.groupBy("rule", "outcome").count().orderBy("rule", "outcome").collect()}
    counts = {name: spark.table(f"{NAMESPACE}.{name}").where(F.col("run_id") == args.run_id).count() for name in TABLES}
    print("BUSINESS " + json.dumps({"run_id": args.run_id, "rows": counts, "checks": outcomes}), flush=True)
    spark.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
