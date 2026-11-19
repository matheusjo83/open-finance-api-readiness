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
- [ ] **7. A second source of consent state**: each consent is read once, so when that read fails the contract layer, its accounts checks are inconclusive. That is 81 of the 89 inconclusive business checks in the [reference campaign](detection-report.md). Candidates: the consent's creation and revocation responses, or a second read as a request of its own (so a draw of its own).
- [ ] **8. Docker layer cache in CI**: the Spark image step takes 60 to 105 seconds, because it downloads the S3A jars (about 280 MB) on every run.
- [ ] **9. Focused campaign** (optional): the rare classes at boosted rates (`DEFECT_RATES`), reported apart from the reference campaign. It would estimate each rule with a narrow interval, but the mix is artificial, so it would not replace the default-rate figures.

## Later

- FAPI-BR security: the OpenID Foundation conformance suite against a local authorization server (Keycloak); mTLS, PAR, JARM, DCR.
- Full consent state machine: extensions (`POST /consents/{consentId}/extends`, `GET /consents/{consentId}/extensions`), multiple approvers, permission-group rules (2a covers creation, authorisation, rejection, revocation and expiry).
- Operational layer: latency and availability per endpoint, call limits (k6), PCM-style metrics (Manual de Monitoramento v3.0, IN BCB 706).
- Payments (Pix), several simulated institutions, orchestration (Airflow), scale benchmark.
