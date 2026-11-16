# Decisions

Tool and architecture choices, in the order they were made. Open source first.

| # | Decision | Chosen | Why / alternatives |
|---|---|---|---|
| 1 | First scope | Consents (3.3.1) + Accounts (2.4.2) | Phase 2 data sharing: consent lifecycle and cross-endpoint consistency. Phase 1 open data was simpler but has no consent or security. Accounts 2.5.0 is still beta. |
| 2 | Transformations | PySpark on Kubernetes | Replaces dbt (used in payer-data-readiness); closer to production data platforms. |
| 3 | Table format | Apache Iceberg 1.12.0 | Engine-neutral; Delta was the alternative. |
| 4 | Iceberg catalog | JDBC catalog in Postgres (CloudNativePG) | Safe for concurrent commits with one small service. REST catalog (Lakekeeper, Polaris) is closer to production but one more service; the Hadoop catalog on S3 is unsafe for concurrent writes. |
| 5 | Object store | SeaweedFS 4.54 (all-in-one, S3 gateway) | Apache 2.0 and active. MinIO's repository was archived (last release 2025-10-15). Garage is AGPL; Ceph RGW too heavy for kind/CI. |
| 6 | Spark | 3.5.9 (Scala 2.12, Java 17) | Long-lived line, most common in production. |
| 7 | Local Kubernetes | kind v0.33.0 (node v1.37.0) | Runs the same way on a laptop and on GitHub runners. |
| 8 | Installs | Helm charts with pinned versions and values files | CloudNativePG 0.29.1, SeaweedFS 4.54.0, Spark Operator 2.6.0. |
| 9 | Mock transmitter | Same approach as the official [mock-api](https://github.com/OpenBanking-Brasil/mock-api): [openapi-mock](https://github.com/muonsoft/openapi-mock) on the official specs, plus a defect proxy | The official images cover Phase 1 only and are frozen at v1.0.0-rc5.3 (2024). The proxy injects the catalogued defects the validators must detect. |
