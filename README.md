# open-finance-api-readiness

Architecture and processes to validate regulated interoperability APIs, starting with
[Open Finance Brasil](https://openfinancebrasil.org.br/): the official OpenAPI specifications,
a mock transmitter with defects injected on purpose, and validators in layers (specification,
contract, business rules, later security and operations) whose detection is measured.

The data pipeline is PySpark on Kubernetes, with Iceberg tables in an S3 object store.
Everything is open source and runs locally in kind; CI runs the same targets.

See [docs/backlog.md](docs/backlog.md) for the plan and [docs/decisions.md](docs/decisions.md)
for the tool choices.

## Platform

| Component | Version | Role |
|---|---|---|
| kind | v0.33.0 (Kubernetes v1.37.0) | Local cluster: one control plane, one worker |
| Kubeflow Spark Operator | chart 2.6.0 | Runs `SparkApplication`s in the `spark` namespace |
| Spark | 3.5.9 (Scala 2.12, Java 17) | PySpark jobs (`images/spark`) |
| Apache Iceberg | 1.12.0 | Table format, JDBC catalog, S3FileIO |
| CloudNativePG | chart 0.29.1 (operator 1.30.1, Postgres 18.6) | Iceberg catalog database |
| SeaweedFS | chart 4.54.0 | S3 object store (bucket `lakehouse`) |

## Quickstart

Requires Docker and linux-amd64 (WSL works). `kind`, `helm` and `kubectl` are downloaded into
`.bin/` at pinned versions.

```bash
make up      # cluster, platform, Spark image
make smoke   # Iceberg table written to SeaweedFS through the Postgres catalog, read back
make down    # delete the cluster
```

`make diagnose` prints pods, events and logs when something fails.

The credentials in `infra/k8s/dev-credentials.yaml` and `infra/helm/seaweedfs.yaml` are fixed
values for the disposable local cluster only.

## License

MIT
