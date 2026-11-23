# PySpark image for the jobs: Spark 3.5.9 (Scala 2.12, Java 17), Iceberg 1.12.0 with the
# AWS bundle (S3FileIO) and the Postgres JDBC driver (JDBC catalog), and the S3A connector
# (hadoop-aws, matching the bundled Hadoop 3.3.4, with the AWS SDK v1 it was built against) to
# read the validator's and the proxy's JSONL. Jars pinned by SHA-256, readable by the spark user
# through ADD --chmod: a RUN chmod afterwards would copy them into a second layer (400 MB).
# Build context: the repository root (make image).
FROM apache/spark:3.5.9-scala2.12-java17-python3-ubuntu

USER root

ADD --chmod=644 --checksum=sha256:8dd8c90e54ed1a20f2461309d047a255ee56382eb7c0a46162b27a4936fd86dc \
    https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-3.5_2.12/1.12.0/iceberg-spark-runtime-3.5_2.12-1.12.0.jar \
    /opt/spark/jars/
ADD --chmod=644 --checksum=sha256:dd7e49ffa4ac9b215bed846581991259e3aec9bb2fa08d6cec029c46cd66d957 \
    https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-aws-bundle/1.12.0/iceberg-aws-bundle-1.12.0.jar \
    /opt/spark/jars/
ADD --chmod=644 --checksum=sha256:73914527305a40cce504b0d3d90b23caf565136912d607ae8ae7c5895512332c \
    https://repo1.maven.org/maven2/org/postgresql/postgresql/42.7.14/postgresql-42.7.14.jar \
    /opt/spark/jars/
ADD --chmod=644 --checksum=sha256:53f9ae03c681a30a50aa17524bd9790ab596b28481858e54efd989a826ed3a4a \
    https://repo1.maven.org/maven2/org/apache/hadoop/hadoop-aws/3.3.4/hadoop-aws-3.3.4.jar \
    /opt/spark/jars/
ADD --chmod=644 --checksum=sha256:873fe7cf495126619997bec21c44de5d992544aea7e632fdc77adb1a0915bae5 \
    https://repo1.maven.org/maven2/com/amazonaws/aws-java-sdk-bundle/1.12.262/aws-java-sdk-bundle-1.12.262.jar \
    /opt/spark/jars/

COPY jobs/ /opt/jobs/

# The image's spark user.
USER 185
