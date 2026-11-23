# PySpark image for the jobs: Spark 3.5.9 (Scala 2.12, Java 17), Iceberg 1.12.0 with the
# AWS bundle (S3FileIO) and the Postgres JDBC driver (JDBC catalog), and the S3A connector
# (hadoop-aws, matching the bundled Hadoop 3.3.4, with the AWS SDK v1 it was built against) to
# read the validator's and the proxy's JSONL. The jars are fetched beforehand into .cache/jars/
# (make jars: scripts/fetch-jars.sh, pinned by SHA-256 in images/spark/jars.sha256), so CI can
# cache them (decision 36). COPY --chmod makes them readable by the spark user: a RUN chmod
# afterwards would copy them into a second layer (400 MB).
# Build context: the repository root (make image).
FROM apache/spark:3.5.9-scala2.12-java17-python3-ubuntu

USER root

COPY --chmod=644 .cache/jars/*.jar /opt/spark/jars/

COPY jobs/ /opt/jobs/

# The image's spark user.
USER 185
