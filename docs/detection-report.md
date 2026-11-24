# Reference detection report (2026-11-24)

A snapshot of `make campaign SEEDS=3 CUSTOMERS=50 REPORT_NAME=reference-20261124`, taken on
2026-11-24. It covers three runs, defect seeds 1 to 3, with the default rates and 50 synthetic
customers. The numbers hold for that day only ([report.md](report.md#campaigns-and-reproducibility)).
How to read them: [report.md](report.md). The earlier snapshots (v1.0.0 of 2026-11-18, v1.1.0 of
2026-11-19, v1.2.0 of 2026-11-23) are in the history of this file. The focused campaign of the
same day is in [focused-report.md](focused-report.md).

## Reading

- **Precision.** The contract and business layers flagged nothing clean, and nothing from the
  other layer.
- **Contract layer.** It found all 1,592 contract defects. Two of the classes are invisible to
  plain schema validation:
  - `undeclared-field` (381): only `strict.undeclared-field` sees it;
  - `missing-interaction-id` (510): `schema.header` sees 225, the Accounts 2xx responses. The
    other 285 (Consents, renewals included, and the Accounts errors) only
    `interaction-id.echo` finds.
- **Business layer.** Conditional recall is 100 % (998 of 998 conclusive). Strict recall is
  98.0 %, because 20 defects were inconclusive.
- **Consent renewals (decision 38).** The walk now renews a consent per customer twice, reads
  its history and the consent again, and makes two renewals that must be refused. That adds 11
  calls per customer: 10,182 exchanges against 8,532 on 2026-11-23. Both new classes were found
  every time they were conclusive:
  - `broken-extension-history`: 21 positives, 18 found, 3 inconclusive;
  - `extension-not-reflected`: 17 positives, 15 found, 2 inconclusive.

  At the default rate of 0.05 they have few cases (intervals 82–100 % and 80–100 %); the
  focused campaign has 40 and 38. Their inconclusive cases have one cause: the last renewal's
  201 failed the contract layer, so the expected expiration is unknown.
- **Where the inconclusive checks are:** `consent-not-enforced` 10 of 661 (both reads of the
  consent failed the contract layer), `inconsistent-balance` 3 of 44 (no usable history),
  `broken-extension-history` 3 of 21, `extension-not-reflected` 2 of 17, `broken-pagination`
  2 of 143.

## Report

Runs: `reference-20261124-seed1`, `reference-20261124-seed2`, `reference-20261124-seed3`. Exchanges: 10182. Intervals: 95 % Wilson.

### By layer

| Layer | Precision | Recall (strict) | Recall (conditional) | Detected | Inconclusive | Positives |
|---|---|---|---|---|---|---|
| contract | 100% [100%–100%] | 100% [100%–100%] | 100% [100%–100%] | 1592 | 0 | 1592 |
| business | 100% [100%–100%] | 98.0% [97%–99%] | 100% [100%–100%] | 998 | 20 | 1018 |

### By defect class

| Class | Layer | Positives | Detected | Inconclusive | Missed | Recall (strict) | Recall (conditional) |
|---|---|---|---|---|---|---|---|
| `missing-required-field` | contract | 345 | 345 | 0 | 0 | 100% [99%–100%] | 100% [99%–100%] |
| `invalid-format` | contract | 356 | 356 | 0 | 0 | 100% [99%–100%] | 100% [99%–100%] |
| `missing-interaction-id` | contract | 510 | 510 | 0 | 0 | 100% [99%–100%] | 100% [99%–100%] |
| `undeclared-field` | contract | 381 | 381 | 0 | 0 | 100% [99%–100%] | 100% [99%–100%] |
| `inconsistent-balance` | business | 44 | 41 | 3 | 0 | 93.2% [82%–98%] | 100% [91%–100%] |
| `broken-pagination` | business | 143 | 141 | 2 | 0 | 98.6% [95%–100%] | 100% [97%–100%] |
| `consent-not-enforced` | business | 661 | 651 | 10 | 0 | 98.5% [97%–99%] | 100% [99%–100%] |
| `transaction-outside-window` | business | 132 | 132 | 0 | 0 | 100% [97%–100%] | 100% [97%–100%] |
| `broken-extension-history` | business | 21 | 18 | 3 | 0 | 85.7% [65%–95%] | 100% [82%–100%] |
| `extension-not-reflected` | business | 17 | 15 | 2 | 0 | 88.2% [66%–97%] | 100% [80%–100%] |

### By rule

| Rule | Layer | Flagged | Hits on its targets | Precision |
|---|---|---|---|---|
| `schema.body` | contract | 701 | 701 | 100% [99%–100%] |
| `schema.header` | contract | 225 | 225 | 100% [98%–100%] |
| `interaction-id.echo` | contract | 510 | 510 | 100% [99%–100%] |
| `strict.undeclared-field` | contract | 381 | 381 | 100% [99%–100%] |
| `balance.matches-transactions` | business | 41 | 41 | 100% [91%–100%] |
| `pagination.duplicate-item` | business | 95 | 95 | 100% [96%–100%] |
| `pagination.missing-next` | business | 36 | 36 | 100% [90%–100%] |
| `pagination.total-records` | business | 10 | 10 | 100% [72%–100%] |
| `consent.enforced` | business | 651 | 651 | 100% [99%–100%] |
| `transactions.within-window` | business | 132 | 132 | 100% [97%–100%] |
| `extensions.history-consistent` | business | 18 | 18 | 100% [82%–100%] |
| `consent.extension-reflected` | business | 15 | 15 | 100% [80%–100%] |

### Who found what

Exchanges per label (rows) and rule that fired (columns).

| Label | `balance.matches-transactions` | `consent.enforced` | `consent.extension-reflected` | `extensions.history-consistent` | `interaction-id.echo` | `pagination.duplicate-item` | `pagination.missing-next` | `pagination.total-records` | `schema.body` | `schema.header` | `strict.undeclared-field` | `transactions.within-window` |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| `clean` |  |  |  |  |  |  |  |  |  |  |  |  |
| `missing-required-field` |  |  |  |  |  |  |  |  | 345 |  |  |  |
| `invalid-format` |  |  |  |  |  |  |  |  | 356 |  |  |  |
| `missing-interaction-id` |  |  |  |  | 510 |  |  |  |  | 225 |  |  |
| `undeclared-field` |  |  |  |  |  |  |  |  |  |  | 381 |  |
| `inconsistent-balance` | 41 |  |  |  |  |  |  |  |  |  |  |  |
| `broken-pagination` |  |  |  |  |  | 95 | 36 | 10 |  |  |  |  |
| `consent-not-enforced` |  | 651 |  |  |  |  |  |  |  |  |  |  |
| `transaction-outside-window` |  |  |  |  |  |  |  |  |  |  |  | 132 |
| `broken-extension-history` |  |  |  | 18 |  |  |  |  |  |  |  |  |
| `extension-not-reflected` |  |  | 15 |  |  |  |  |  |  |  |  |  |

### Gate

All invariants hold: precision 1 in both layers, conditional recall 1 in every class, no inconclusive contract check.
