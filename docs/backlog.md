# Backlog

The goal: a reproducible pipeline that validates regulated Open Finance Brasil APIs in layers
(specification, contract, business rules, then security and operations) and measures what it
detects, against defects injected on purpose (precision and recall per defect class).

## MVP: Consents + Accounts, layers 1 to 3

- [x] **0. Platform skeleton**: kind, Spark Operator, SeaweedFS, Iceberg catalog in Postgres, `make up` / `make smoke`, CI.
- [x] **1. Specs**: the official OpenAPI files of Consents 3.3.1 and Accounts 2.4.2, pinned, linted with Spectral (`spectral:oas` plus `ofb-*` convention rules); known findings in [spec-findings.md](spec-findings.md).
- [x] **2a. Synthetic transmitter**: Consents 3.3.1 and Accounts 2.4.2 over a seeded, coherent dataset with consent state; every response validated against the pinned specs ([transmitter.md](transmitter.md)).
- [x] **2b. Defect proxy**: in front of the transmitter, 8 defect classes (4 contract, 4 business) chosen deterministically per request, every exchange labelled (ground truth) as JSONL in the object store ([defects.md](defects.md)).
- [x] **3. Contract validator**: walks the seeded resources through the proxy, checks every response against the pinned specs (openapi-core) plus an `x-fapi-interaction-id` echo rule and a strict rule for undeclared fields; results and exchanges as JSONL in the object store, keyed like the labels ([validator.md](validator.md)).
- [x] **4. Business rules job**: PySpark reads the exchanges the validator recorded (S3A), skips what the contract layer failed, applies one rule per business defect class, writes bronze, silver and gold Iceberg tables partitioned by run ([business-rules.md](business-rules.md)).
- [x] **5. Detection report**: precision per layer and per rule, strict and conditional recall per class (inconclusive apart), 95 % Wilson intervals, who-found-what matrix; Iceberg tables and Markdown; gate on the catalogue's invariants in CI; multi-seed campaigns ([report.md](report.md), [detection-report.md](detection-report.md)).

Released as [v1.0.0](https://github.com/matheusjo83/open-finance-api-readiness/releases/tag/v1.0.0).

## After the MVP

- [x] **6. Volume for the rare classes**: every customer gets the non-happy consents, the walk uses them on the accounts list and every balance, and new consents re-read the balances (`BALANCE_CONSENTS`). In the reference campaign, `consent-not-enforced` goes from 3 to 661 positives and `inconsistent-balance` from 10 to 40. The campaign also caught a pagination case that said pass without evidence (decision 31). Released as [v1.1.0](https://github.com/matheusjo83/open-finance-api-readiness/releases/tag/v1.1.0).
- [x] **7. A second source of consent state**: each consent was read once, so when that read failed the contract layer, its accounts checks were inconclusive: 81 of the 89 inconclusive business checks in v1.1.0's reference campaign. The walk now reads each consent twice before use, the second time with a renewed client-credentials token, so the second read gets a draw of its own (decision 33). Released as [v1.2.0](https://github.com/matheusjo83/open-finance-api-readiness/releases/tag/v1.2.0).
- [x] **8. Spark image step in CI**: it took 60 to 105 seconds. The jars were thought to be the cause, but measured they take about 3 seconds to download; most of the step was `kind load` of a 3.4 GB image into both nodes. The jars now get their mode from `ADD --chmod` (no second 400 MB layer, 2.6 GB image), and every image is loaded into the worker only (decision 34). A layer or jar cache was left out: it would save seconds, not tens of them.

Next release (v1.3.0): items 10, 12, 9 and 11, in that order, with a new reference campaign.

- [x] **9. Focused campaign**: the rare classes at boosted rates, the contract classes off, reported apart from the reference campaign ([focused-report.md](focused-report.md), decision 37). Each business rule now has 55 to 534 cases; conditional recall stays at 100 %. The campaign also showed the business-rules job short of memory, which got the report's overhead.
- [x] **10. Memory of the detection report**: in the reference campaign of 2026-11-23 one report executor was OOMKilled (Spark retried the task). Its pod peaked at 1051 MiB of 1433 MiB on a rerun: 1g of heap plus the default 384m overhead. The overhead is now 768m for the driver and the executor, the heap unchanged; the rerun peaked at 981 MiB of 1792 MiB, with the same report (decision 35).
- [x] **11. Consent renewal**: `POST /consents/{consentId}/extends` and `GET /consents/{consentId}/extensions` in the transmitter, the walk and the validator, two business classes (`broken-extension-history`, `extension-not-reflected`) with a rule each (decision 38). Multiple approvers and the permission-group rules need business customers and stay for later (below).
- [x] **12. Jars of the Spark image cached in CI**: Maven Central answered every jar with one wrong body on 3 of 6 CI runs on 2026-11-23 (the checksums rejected it). The jars are now fetched by `make jars` with a mirror and retries, and CI caches them (decision 36).

## Later

- FAPI-BR security: the OpenID Foundation conformance suite against a local authorization server (Keycloak); mTLS, PAR, JARM, DCR.
- Rest of the consent state machine: multiple approvers (`DEPENDE_MULTIPLA_ALCADA`) and the permission-group rules (`COMBINACAO_PERMISSOES_INCORRETA`), with business customers (CNPJ) in the dataset.
- Operational layer: latency and availability per endpoint, call limits (k6), PCM-style metrics (Manual de Monitoramento v3.0, IN BCB 706).
- Payments (Pix), several simulated institutions, orchestration (Airflow), scale benchmark.
