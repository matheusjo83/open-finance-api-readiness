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
| Defect proxy | Python 3.13, Starlette, boto3 (`services/proxy`, uv.lock) | Injects catalogued defects, labels every exchange and reports it as the server, in S3 |
| Contract validator | Python 3.13, openapi-core, boto3 (`services/validator`, uv.lock) | Checks every response through the proxy, results and the client's PCM reports in S3 |
| Business rules job | PySpark 3.5.9 (`jobs/business`) | Domain rules over the validator's exchanges, Iceberg tables in `lake.ofb` |
| Detection report | PySpark 3.5.9 (`jobs/report`) | Precision and recall per layer, class and rule against the labels |
| History | PySpark 3.5.9 (`jobs/history`) | 90 days of PCM reports of both sides with operational defects, and their labels |
| Operational job | PySpark 3.5.9 (`jobs/operational`) | The operational metrics per day and month, from the reports of both sides |

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

The reference campaign of 2026-11-24 had 3 defect seeds and 50 synthetic customers, 10,182
exchanges in all ([docs/detection-report.md](docs/detection-report.md)):

| Layer | Precision | Recall (strict) | Recall (conditional) | Defects |
|---|---|---|---|---|
| Contract | 100 % [100–100] | 100 % [100–100] | 100 % | 1,592 |
| Business | 100 % [100–100] | 98.0 % [97–99] | 100 % | 1,018 (20 inconclusive) |

Intervals: 95 % Wilson. Two contract defects pass plain schema validation:

- an undeclared field: only the strict rule finds it;
- a missing `x-fapi-interaction-id` on Consents and on Accounts errors: only the echo rule finds
  it.

An inconclusive business check is one whose input the contract layer left out: both reads of a
consent (10 of 661 `consent-not-enforced`), part of an account's history (3 of 44
`inconsistent-balance`), or the response to the last renewal (5 of 38 for the two renewal
classes).

A focused campaign the same day, with the contract defects off and the business defects
boosted, measures each business rule on 38 to 464 cases: precision and conditional recall stay
at 100 % ([docs/focused-report.md](docs/focused-report.md)). Its mix is artificial, so it
complements the reference figures above and does not replace them.

The operational layer is measured on generated histories: five seeds of 90 days, 11 endpoints,
464,198 exchanges ([docs/operational-report.md](docs/operational-report.md)). Every verdict
flags only units with a cause the requirements allow and finds every unit built for it:

| Verdict | Unit | Flagged | Positives | Precision | Recall |
|---|---|---|---|---|---|
| Response time (P95) | endpoint-day | 181 | 181 | 100 % [98–100] | 100 % [98–100] |
| Availability | endpoint-day | 126 | 126 | 100 % [97–100] | 100 % [97–100] |
| 90-day availability | endpoint | 24 | 16 | 100 % [86–100] | 100 % [81–100] |
| Overload (529) | institution-day | 30 | 17 | 100 % [89–100] | 100 % [82–100] |
| Reports by 08:00 / within 7 days | API-day | 47 / 53 | 47 / 53 | 100 % | 100 % |
| Monthly verdicts | month | 240 | 198 | 100 % | 100 % |

The 143 units built exactly at a threshold (P95 equal to the SLA, a minute or a day at 95 %, 529s
at 0.5 %) all come out as the requirements say.

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

`specs/operational/requirements.json` holds the operational requirements of these APIs (response
time, availability, limits, PCM reporting), each number with its source and the disagreements
between sources with the reading chosen ([docs/operational.md](docs/operational.md)).

```bash
make check-requirements   # against the pinned specs and the manual's worked examples
```

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

`services/proxy` (`http://proxy.ofb.svc`) forwards to the transmitter and injects one of 10
catalogued defects (4 contract, 6 business) into a share of the responses, chosen
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

## Telemetry

The validator and the proxy also report every exchange as the client and the server would to the
PCM (Plataforma de Coleta de Métricas), timed on each side, in `s3://lakehouse/pcm/client/` and
`s3://lakehouse/pcm/server/` under the run id. The operational metrics are computed from them.

```bash
make telemetry-smoke   # pairs the last run's reports of both sides, prints the client's P95
```

See [docs/telemetry.md](docs/telemetry.md) for what is measured, the fields and where the PCM's
OpenAPI file and the portal disagree.

## History

`jobs/history` (a SparkApplication) generates 90 days of PCM reports of both sides, in the same
format, with operational defects built in: slow days, outages, overload, reports late, missing or
inconsistent, and days exactly at a threshold. Each episode is labelled per endpoint, API and day
in `s3://lakehouse/pcm-labels/`.

```bash
make history   # 90 days, 30 minutes x 3 requests per endpoint and day (HISTORY_* to change)
```

See [docs/history.md](docs/history.md) for the classes, the edges and how the labels are built.

## Operational metrics

`jobs/operational` (a SparkApplication) pairs a run's PCM reports as the PCM does and computes,
per endpoint, API and institution, per day and per month: the P95 against the SLA, the daily and
90-day availability, the share of 529 and the reporting goals. Over a history it reproduces
every label.

```bash
make operational         # the proxy's current run, into lake.ofb.operational_*
make operational-smoke   # the CI history: labels checked, detection report gated
make operational-campaign SEEDS=5 OPERATIONAL_REPORT=x   # a history per seed, one report over all
```

See [docs/operational-job.md](docs/operational-job.md) for the metrics and
[docs/operational-report.md](docs/operational-report.md) for the detection report.

## Limits

The transmitter applies the traffic (TPM) and monthly limits at the minimums; a second proxy
(`proxy-limits`) refuses too early or where no limit applies, drawn per test case; k6 drives 34
test cases through it (about 18,600 requests in a minute). `jobs/limits` (a SparkApplication)
judges each 429 and 423 from the client's log alone: early against the requests of the 60
seconds before it, early against the counted calls of the Brasília month, or on Consents.

```bash
make limits limits-smoke   # one run (LIMITS_SEED, default 42) and its checks
make limits-report         # the job over it, the detection report gated
make limits-campaign SEEDS=5 LIMITS_REPORT=x   # a run per seed, one report over all
```

See [docs/limits.md](docs/limits.md) for the load and [docs/limits-job.md](docs/limits-job.md)
for the verdicts and the detection report.

## Security (FAPI-BR)

In progress (backlog item 19, stage 1 of 4). The transmitter's authorisation server is Keycloak
26.8.2 with a realm set for FAPI-BR v2.2.0 (FAPI 1 Advanced: private_key_jwt, PAR, PKCE, PS256,
encrypted id_token, certificate-bound tokens, `consent:<id>`), behind a PKI of our own; the
OpenID Foundation's conformance suite (release-v5.3.5) runs its Brazil plan against it headless,
in the `security` namespace. The clean baseline: 54 of 67 modules passed, 8 known outcomes (5 of
Keycloak itself), 4 for review with evidence, none unexpected, in about 2.6 minutes.

```bash
make security-test                 # the PKI, the realm and the plan configuration
make security-deploy conformance   # Keycloak and the suite, then the plan, gated
```

See [docs/security.md](docs/security.md) for the realm against FAPI-BR v2.2.0, the run and the
known outcomes.

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
make focused-campaign REPORT_NAME=x  # the same, with the business defects boosted
```

See [docs/report.md](docs/report.md) for the metrics and
[docs/detection-report.md](docs/detection-report.md) for the latest reference campaign.

## License

MIT
