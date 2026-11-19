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
| Spark | 3.5.9 (Scala 2.12, Java 17), S3A (hadoop-aws 3.3.4) | PySpark jobs (`images/spark`) |
| Apache Iceberg | 1.12.0 | Table format, JDBC catalog, S3FileIO |
| CloudNativePG | chart 0.29.1 (operator 1.30.1, Postgres 18.6) | Iceberg catalog database |
| SeaweedFS | chart 4.54.0 | S3 object store (bucket `lakehouse`) |
| Synthetic transmitter | Python 3.13, FastAPI (`services/transmitter`, uv.lock) | Consents and Accounts APIs over a seeded dataset |
| Defect proxy | Python 3.13, Starlette, boto3 (`services/proxy`, uv.lock) | Injects catalogued defects, labels every exchange in S3 |
| Contract validator | Python 3.13, openapi-core, boto3 (`services/validator`, uv.lock) | Checks every response through the proxy, results in S3 |
| Business rules job | PySpark 3.5.9 (`jobs/business`) | Domain rules over the validator's exchanges, Iceberg tables in `lake.ofb` |
| Detection report | PySpark 3.5.9 (`jobs/report`) | Precision and recall per layer, class and rule against the labels |

## Quickstart

Requires Docker and linux-amd64 (WSL works). `kind`, `helm`, `kubectl` and `uv` are downloaded
into `.bin/` at pinned versions.

```bash
make up              # cluster, platform, Spark image, synthetic transmitter, defect proxy
make smoke           # Iceberg table written to SeaweedFS through the Postgres catalog, read back
make validate        # one contract validation run through the proxy (a Job), results in SeaweedFS
make business-rules  # business rules over that run (a SparkApplication), Iceberg tables
make report          # precision and recall of both layers over that run, against the labels
make down            # delete the cluster
```

`make diagnose` prints pods, events and logs when something fails. `make reports` collects the
current run's data (labels, contract results, exchanges, detection report) and the jobs' logs
into `reports/`. CI uploads it as the `reports` artifact.

## Results

The reference campaign of 2026-11-19 had 3 defect seeds and 50 synthetic customers, 7,479
exchanges in all ([docs/detection-report.md](docs/detection-report.md)):

| Layer | Precision | Recall (strict) | Recall (conditional) | Defects |
|---|---|---|---|---|
| Contract | 100 % [100–100] | 100 % [100–100] | 100 % | 1,126 |
| Business | 100 % [100–100] | 90.8 % [89–92] | 100 % | 971 (89 inconclusive) |

Intervals: 95 % Wilson. Two contract defects pass plain schema validation:

- an undeclared field: only the strict rule finds it;
- a missing `x-fapi-interaction-id` on Consents and on Accounts errors: only the echo rule finds
  it.

Most inconclusive business checks are of `consent-not-enforced` (81 of 661): the consent's one
read failed the contract layer, so its state is unknown.

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

## Defect proxy

`services/proxy` (`http://proxy.ofb.svc`) forwards to the transmitter and injects one of 8
catalogued defects (4 contract, 4 business) into a share of the responses, chosen
deterministically from a seed and the request. Every exchange gets a label (ground truth) in
`s3://lakehouse/labels/run=<run_id>/`. `make up` deploys it.

```bash
make proxy-test    # every defect class against the real transmitter, in process
make proxy-smoke   # walks the seeded resources through the proxy, reads the labels back from S3
```

See [docs/defects.md](docs/defects.md) for the catalogue, the selection and the label format.

## Contract validator

`services/validator` walks the seeded resources through the proxy, as a Job in the `ofb`
namespace, and checks every response against the pinned specs. It runs the schema rules
(openapi-core), plus an `x-fapi-interaction-id` echo rule and a strict rule for undeclared
fields. Results and the exchanges themselves go to `s3://lakehouse/contract/run=<run_id>/` and
`s3://lakehouse/exchanges/run=<run_id>/`, under the proxy's run id.

```bash
make validator-test    # every defect class through the proxy, in process
make validate          # one run in the cluster
make validator-smoke   # joins its results with the proxy's labels, prints the outcome per class
```

See [docs/validator.md](docs/validator.md) for the walk, the rules and the result format.

## Business rules job

`jobs/business` is a PySpark job (SparkApplication) over one validation run. It reads the
validator's exchange log and contract results through S3A and leaves out what the contract layer
failed. It applies one rule per business defect class: balance against transactions,
pagination, consent enforcement and booking window. It writes bronze, silver and gold Iceberg
tables in `lake.ofb`, partitioned by run.

```bash
make jobs-test        # business rules and report, local Spark over exported runs (needs Java 17)
make business-rules   # the job over the proxy's current run (RUN_ID=... for another)
```

See [docs/business-rules.md](docs/business-rules.md) for the rules, the gating and the tables.

## Detection report

`jobs/report` joins one or more runs with the proxy's labels and measures each layer. It
reports precision per layer and per rule, and strict and conditional recall per defect class
(inconclusive counted apart), with 95 % Wilson intervals and a matrix of who found what. The
results go to Iceberg tables (`lake.ofb.detection_*`) and to a Markdown report in
`s3://lakehouse/reports/<name>/`. In CI it gates the catalogue's invariants.

```bash
make report                                       # the proxy's current run
make report-smoke                                 # the same, failing if an invariant breaks
make campaign SEEDS=3 CUSTOMERS=50 REPORT_NAME=x  # one run per defect seed, reported together
```

See [docs/report.md](docs/report.md) for the metrics and
[docs/detection-report.md](docs/detection-report.md) for the latest reference campaign.

## License

MIT
