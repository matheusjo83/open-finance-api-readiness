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
- [ ] **4. Business rules job**: PySpark reads the exchanges the validator recorded, applies the rules, writes findings to Iceberg tables.
- [ ] **5. Detection report**: precision and recall per defect class.

## Later

- FAPI-BR security: the OpenID Foundation conformance suite against a local authorization server (Keycloak); mTLS, PAR, JARM, DCR.
- Full consent state machine: extensions (`POST /consents/{consentId}/extends`, `GET /consents/{consentId}/extensions`), multiple approvers, permission-group rules (2a covers creation, authorisation, rejection, revocation and expiry).
- Operational layer: latency and availability per endpoint, call limits (k6), PCM-style metrics (Manual de Monitoramento v3.0, IN BCB 706).
- Payments (Pix), several simulated institutions, orchestration (Airflow), scale benchmark.
