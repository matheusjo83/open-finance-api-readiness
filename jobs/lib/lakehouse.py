"""Spark session wired to the lakehouse: Iceberg tables in SeaweedFS (S3), catalog in Postgres.

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
    }
    builder = SparkSession.builder.appName(app_name)
    for key, value in conf.items():
        builder = builder.config(key, value)
    return builder.getOrCreate()
