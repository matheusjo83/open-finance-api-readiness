"""Platform smoke test: append rows to an Iceberg table and read them back.

Exercises the whole path a real job uses: the Spark Operator, executors writing
Parquet to SeaweedFS through S3FileIO, and the commit in the Postgres JDBC catalog.
Exits non-zero (the SparkApplication fails) if the rows read differ from the rows written.
"""

import json
import sys
import uuid

from pyspark.sql import functions as F

from lakehouse import CATALOG, session

ROWS = 1000
TABLE = f"{CATALOG}.smoke.ping"


def main() -> int:
    spark = session("smoke")
    run_id = str(uuid.uuid4())

    spark.sql(f"CREATE NAMESPACE IF NOT EXISTS {CATALOG}.smoke")
    spark.sql(
        f"CREATE TABLE IF NOT EXISTS {TABLE} "
        "(run_id STRING, n BIGINT, written_at TIMESTAMP) USING iceberg"
    )
    (
        spark.range(ROWS)
        .select(
            F.lit(run_id).alias("run_id"),
            F.col("id").alias("n"),
            F.current_timestamp().alias("written_at"),
        )
        .writeTo(TABLE)
        .append()
    )

    read = spark.table(TABLE).where(F.col("run_id") == run_id)
    result = {
        "run_id": run_id,
        "written": ROWS,
        "read": read.count(),
        "distinct_n": read.select("n").distinct().count(),
        "snapshots": spark.sql(f"SELECT count(*) AS c FROM {TABLE}.snapshots").first()["c"],
    }
    print("SMOKE " + json.dumps(result))
    spark.stop()

    ok = result["read"] == ROWS and result["distinct_n"] == ROWS and result["snapshots"] >= 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
