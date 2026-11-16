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
| Synthetic transmitter | Python 3.13, FastAPI (`services/transmitter`, uv.lock) | Consents and Accounts APIs over a seeded dataset |

## Quickstart

Requires Docker and linux-amd64 (WSL works). `kind`, `helm`, `kubectl` and `uv` are downloaded
into `.bin/` at pinned versions.

```bash
make up      # cluster, platform, Spark image, synthetic transmitter
make smoke   # Iceberg table written to SeaweedFS through the Postgres catalog, read back
make down    # delete the cluster
```

`make diagnose` prints pods, events and logs when something fails.

## Specifications

| API | Version | Source |
|---|---|---|
| Consents | 3.3.1 | `swagger-apis/consents/3.3.1.yml` |
| Accounts | 2.4.2 | `swagger-apis/accounts/2.4.2.yml` |

Both come from [OpenBanking-Brasil/openapi](https://github.com/OpenBanking-Brasil/openapi) at the
commit pinned in the `Makefile`, checked by SHA-256. They are downloaded into `specs/upstream/`
(not committed: the upstream repository declares no license).

```bash
make lint-specs   # Spectral: spectral:oas + the ofb-* rules in specs/ruleset.yaml
```

The lint fails on errors and prints warnings; the known warnings are listed in
[docs/spec-findings.md](docs/spec-findings.md). It also checks that every `ofb-*` rule fires on
`specs/fixtures/defective.yml`, a spec built to break each of them. Only Docker is needed.

The credentials in `infra/k8s/dev-credentials.yaml` and `infra/helm/seaweedfs.yaml` are fixed
values for the disposable local cluster only.

## Synthetic transmitter

`services/transmitter` serves Consents 3.3.1 and Accounts 2.4.2 over a seeded synthetic dataset,
in the `ofb` namespace (`http://transmitter.ofb.svc`). `make up` deploys it.

```bash
make transmitter-test    # every response, happy and error paths, validated against the specs
make transmitter-smoke   # calls it through its Service from inside the cluster
```

See [docs/transmitter.md](docs/transmitter.md) for the dataset, the consent lifecycle and the
mock-only stand-ins (token, authorisation).

## License

MIT
