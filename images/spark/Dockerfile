# PySpark image for the jobs: Spark 3.5.9 (Scala 2.12, Java 17), Iceberg 1.12.0 with the
# AWS bundle (S3FileIO) and the Postgres JDBC driver (JDBC catalog). Jars pinned by SHA-256.
# Build context: the repository root (make image).
FROM apache/spark:3.5.9-scala2.12-java17-python3-ubuntu

USER root

ADD --checksum=sha256:8dd8c90e54ed1a20f2461309d047a255ee56382eb7c0a46162b27a4936fd86dc \
    https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-3.5_2.12/1.12.0/iceberg-spark-runtime-3.5_2.12-1.12.0.jar \
    /opt/spark/jars/
ADD --checksum=sha256:dd7e49ffa4ac9b215bed846581991259e3aec9bb2fa08d6cec029c46cd66d957 \
    https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-aws-bundle/1.12.0/iceberg-aws-bundle-1.12.0.jar \
    /opt/spark/jars/
ADD --checksum=sha256:73914527305a40cce504b0d3d90b23caf565136912d607ae8ae7c5895512332c \
    https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.14/postgresql-42.7.14.jar \
    /opt/spark/jars/
RUN chmod 644 /opt/spark/jars/*.jar

COPY jobs/ /opt/jobs/

# The image's spark user.
USER 185
