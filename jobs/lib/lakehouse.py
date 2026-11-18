"""Spark session wired to the lakehouse: Iceberg tables in SeaweedFS (S3), catalog in Postgres,
and s3a:// paths (the JSONL the proxy and the validator write) through the S3A connector.

Settings come from the environment, filled by the lakehouse-config ConfigMap and the
lakehouse-credentials Secret (infra/k8s/). Shipped to the jobs through deps.pyFiles.
"""

import os

from pyspark.sql import SparkSession

CATALOG = "lake"


def session(app_name: str) -> SparkSession:
    env = os.environ
    prefix = f"spark.sql.catalog.{CATALOG}"
    conf = {
        "spark.sql.extensions": "org.apache.iceberg.spark.extensions.IcebergSparkSessionExtensions",
        "spark.sql.defaultCatalog": CATALOG,
        prefix: "org.apache.iceberg.spark.SparkCatalog",
        f"{prefix}.catalog-impl": "org.apache.iceberg.jdbc.JdbcCatalog",
        f"{prefix}.uri": env["CATALOG_URI"],
        f"{prefix}.jdbc.user": env["CATALOG_USER"],
        f"{prefix}.jdbc.password": env["CATALOG_PASSWORD"],
        f"{prefix}.warehouse": env["WAREHOUSE"],
        f"{prefix}.io-impl": "org.apache.iceberg.aws.s3.S3FileIO",
        f"{prefix}.s3.endpoint": env["S3_ENDPOINT"],
        f"{prefix}.s3.path-style-access": "true",
        f"{prefix}.s3.access-key-id": env["S3_ACCESS_KEY_ID"],
        f"{prefix}.s3.secret-access-key": env["S3_SECRET_ACCESS_KEY"],
        f"{prefix}.client.region": env["AWS_REGION"],
        # S3A, for spark.read on s3a://lakehouse/... (labels, contract, exchanges).
        "spark.hadoop.fs.s3a.endpoint": env["S3_ENDPOINT"],
        "spark.hadoop.fs.s3a.endpoint.region": env["AWS_REGION"],
        "spark.hadoop.fs.s3a.path.style.access": "true",
        "spark.hadoop.fs.s3a.connection.ssl.enabled": str(env["S3_ENDPOINT"].startswith("https")).lower(),
        "spark.hadoop.fs.s3a.access.key": env["S3_ACCESS_KEY_ID"],
        "spark.hadoop.fs.s3a.secret.key": env["S3_SECRET_ACCESS_KEY"],
        "spark.hadoop.fs.s3a.aws.credentials.provider": "org.apache.hadoop.fs.s3a.SimpleAWSCredentialsProvider",
    }
    builder = SparkSession.builder.appName(app_name)
    for key, value in conf.items():
        builder = builder.config(key, value)
    return builder.getOrCreate()


def replace_partitions(spark: SparkSession, table: str, df, column: str = "run_id") -> None:
    """Writes df to an Iceberg table partitioned by `column`, replacing the partitions it holds;
    creates the table on the first write. Reruns therefore do not duplicate rows."""
    from pyspark.sql import functions as F

    if spark.catalog.tableExists(table):
        df.writeTo(table).overwritePartitions()
    else:
        df.writeTo(table).partitionedBy(F.col(column)).create()
