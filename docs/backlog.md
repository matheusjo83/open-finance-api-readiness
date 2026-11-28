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

Items 8 to 12 were released together as [v1.3.0](https://github.com/matheusjo83/open-finance-api-readiness/releases/tag/v1.3.0), with the reference and focused campaigns of 2026-11-24.

- [x] **9. Focused campaign**: the rare classes at boosted rates, the contract classes off, reported apart from the reference campaign ([focused-report.md](focused-report.md), decision 37). Each business rule now has 55 to 534 cases; conditional recall stays at 100 %. The campaign also showed the business-rules job short of memory, which got the report's overhead.
- [x] **10. Memory of the detection report**: in the reference campaign of 2026-11-23 one report executor was OOMKilled (Spark retried the task). Its pod peaked at 1051 MiB of 1433 MiB on a rerun: 1g of heap plus the default 384m overhead. The overhead is now 768m for the driver and the executor, the heap unchanged; the rerun peaked at 981 MiB of 1792 MiB, with the same report (decision 35).
- [x] **11. Consent renewal**: `POST /consents/{consentId}/extends` and `GET /consents/{consentId}/extensions` in the transmitter, the walk and the validator, two business classes (`broken-extension-history`, `extension-not-reflected`) with a rule each (decision 38). Multiple approvers and the permission-group rules need business customers and stay for later (below).
- [x] **12. Jars of the Spark image cached in CI**: Maven Central answered every jar with one wrong body on 3 of 6 CI runs on 2026-11-23 (the checksums rejected it). The jars are now fetched by `make jars` with a mirror and retries, and CI caches them (decision 36).

## Operational layer

Designed on 2026-11-25. The SLAs are measured per minute, day and 90 days and a run lasts
minutes, so the live capture and the history are separate. Items 13 to 16 and 18 make up v1.4.0
(measurement); item 17 changes the transmitter and is left for v1.5.0 (limits).

- [x] **13. Requirements**: response time, availability, limits and PCM reporting per endpoint of the pinned APIs, each number with its source and version, the disagreements between sources with the reading chosen, checked against the specs and the manual's worked examples ([operational.md](operational.md), decisions 39 and 40).
- [x] **14. Telemetry**: the validator (client) and the proxy (server) write one PCM report per exchange, paired by `x-fapi-interaction-id` and checked in the cluster (`make telemetry-smoke`); the PCM's OpenAPI file pinned by commit. Where that file and the portal disagree, the reports follow the portal ([telemetry.md](telemetry.md), decision 41).
- [x] **15. 90-day history**: a seeded PySpark generator of 90 days of reports of both sides, in the live format, with six operational classes and four edges built in and labelled per endpoint, API and day; the reports recount to the labels ([history.md](history.md), decision 42).
- [x] **16. Operational job**: PySpark pairs the reports as the PCM does and computes the P95, the daily and 90-day availability, the share of 529 and the reporting goals per day and per month into `lake.ofb.operational_*`; over a history it reproduces every label, which CI checks (`make operational-smoke`) ([operational-job.md](operational-job.md), decision 43).
- [x] **17. Limits** (v1.5.0): the transmitter applies the traffic and monthly limits at the minimums; a proxy of its own refuses too early or where no limit applies, drawn per test case; k6 drives 34 test cases through it; PySpark judges each 429 and 423 from the client's log alone (early by the 60 seconds before it, early by the counted calls of the Brasília month, any on Consents), with its detection report gated in CI and over a twenty-seed reference campaign ([limits.md](limits.md), [limits-job.md](limits-job.md), decisions 45 to 47).
- [x] **18. Report**: precision and recall of each operational verdict against the history's causes, per its own unit, with the edges, apart from the other layers; gated in CI and over a five-seed reference campaign ([operational-report.md](operational-report.md), decision 44).

Items 13 to 16 and 18 were released together as [v1.4.0](https://github.com/matheusjo83/open-finance-api-readiness/releases/tag/v1.4.0), with the operational reference campaign of 2026-11-26. Item 17 was released as [v1.5.0](https://github.com/matheusjo83/open-finance-api-readiness/releases/tag/v1.5.0), with the limits reference campaign of 2026-11-27.

### Found along the way

Small items, to take with the next release that touches them:

- **Consent ids at random**: the transmitter creates consents with `uuid4`, so the defects drawn for created consents differ from run to run (a proxy test failed 1 run in 16 on it, fixed in PR #22). Seeded ids would make whole runs reproducible, but they change every draw of the campaigns, so it is a decision of its own.
- **`make proxy-smoke` right after a rollout** sometimes times out connecting (the old pod still behind the Service), with the image of main too. CI deploys once, so it does not fail there; a retry in the script would fix it.
- **Timeouts**: a call the client gives up on after 15 seconds should be reported with 408, and the proxy's upstream timeout (10 seconds) answers 500, not the manual's 504. No defect delays responses yet; item 17 or a latency class would need both.
- **Every minute of the day**: the history at full volume (about 57 million reports) is the scale benchmark below.

## Security layer

Designed on 2026-11-27 (decision 48), for v2.0.0: it changes the base (real OAuth flows and mTLS
in the walk).

- [ ] **19. FAPI-BR** ([security.md](security.md)): the transmitter's authorisation server and its detection.
  - [x] Stage 1: a PKI of our own, Keycloak 26.8.2 with a realm set for FAPI-BR v2.2.0, the OpenID conformance suite's Brazil plan headless against it with a stand-in resource server, gated on the known outcomes in CI. Clean baseline: 54 of 67 modules passed, 8 known (5 of Keycloak itself), 4 for review with evidence, none unexpected, in 158 s.
  - [x] Stage 2a: the transmitter as resource server behind an mTLS gateway (token signature, audience, `cnf.x5t#S256`, introspection, `consent:<id>`, the client's own consents, revocation), the organisation id from the certificate instead of `x-mock-client-org-id`, the consent approved by a Keycloak script mapper as the token is issued. Clean baseline: 56 of 67 passed, 6 known (all of Keycloak), 4 for review, none unexpected, in 175 s.
  - [x] Stage 2b: the walk with real flows (client_credentials, PAR, PKCE, the scripted login and grant, both id_tokens decrypted and checked) through the gateway and the defect proxy, its customers as Keycloak users, the proxy's draw keyed on the consent (and a repeat's occurrence) instead of the bearer, the origin from the certificate; Keycloak and the gateway in the platform job. The stand-ins stay for the tests in process and the limits' k6 load (decision 50). Seed 42: 332 exchanges, 35 logins, report gate ok.
  - [ ] Stage 3: defect variants of the realm (PAR or PKCE optional, RS256, long tokens, refresh rotation, unencrypted id_token, reusable request_uri) and of the resource server (unbound, another consent's or expired tokens accepted), labelled.
  - [ ] Stage 4: the detection report per variant against the baseline, gated, and a campaign.

## Later

Each one is a release of its own, designed before it is built. Suggested order after item 19: the rest of the consent state machine, then Pix and several institutions.

- Rest of the consent state machine: multiple approvers (`DEPENDE_MULTIPLA_ALCADA`) and the permission-group rules (`COMBINACAO_PERMISSOES_INCORRETA`), with business customers (CNPJ) in the dataset.
- Payments (Pix), several simulated institutions, orchestration (Airflow), scale benchmark.
