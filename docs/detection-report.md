# Reference detection report (2026-11-18)

A snapshot of `make campaign SEEDS=3 CUSTOMERS=50 REPORT_NAME=reference-20261118`, taken on
2026-11-18. It covers three runs, defect seeds 1 to 3, with the default rates and 50 synthetic
customers. The numbers hold for that day only ([report.md](report.md#campaigns-and-reproducibility)).
How to read them: [report.md](report.md).

## Reading

- **Precision.** The contract and business layers flagged nothing clean, and nothing from the
  other layer.
- **Contract layer.** It found all 692 contract defects. Two of the classes are invisible to
  plain schema validation, and the rules added in item 3 catch them:
  - `undeclared-field`: only `strict.undeclared-field` sees it;
  - `missing-interaction-id`: 184 cases, of which `schema.header` sees 175. The other 9 are on
    Consents and Accounts errors, where only `interaction-id.echo` finds them.
- **Business layer.** It found 295 of 296. The remaining `inconsistent-balance` was
  inconclusive, not missed: no usable history for that account (a page failed by the contract
  layer or one with a repeated item). Strict recall
  99.7 %, conditional 100 %.
- **Few positives.** `consent-not-enforced` (3) and `inconsistent-balance` (10) have few cases:
  - the walk uses non-authorised consents for the first customer only;
  - it reads one balance per account.

  Their intervals are wide (44–100 %, 60–98 %). More volume for these two would need the walk to
  try the non-authorised consents of every customer.

## Report

Runs: `reference-20261118-seed1`, `reference-20261118-seed2`, `reference-20261118-seed3`. Exchanges: 3798. Intervals: 95 % Wilson.

### By layer

| Layer | Precision | Recall (strict) | Recall (conditional) | Detected | Inconclusive | Positives |
|---|---|---|---|---|---|---|
| contract | 100% [99%–100%] | 100% [99%–100%] | 100% [99%–100%] | 692 | 0 | 692 |
| business | 100% [99%–100%] | 99.7% [98%–100%] | 100% [99%–100%] | 295 | 1 | 296 |

### By defect class

| Class | Layer | Positives | Detected | Inconclusive | Missed | Recall (strict) | Recall (conditional) |
|---|---|---|---|---|---|---|---|
| `missing-required-field` | contract | 176 | 176 | 0 | 0 | 100% [98%–100%] | 100% [98%–100%] |
| `invalid-format` | contract | 153 | 153 | 0 | 0 | 100% [98%–100%] | 100% [98%–100%] |
| `missing-interaction-id` | contract | 184 | 184 | 0 | 0 | 100% [98%–100%] | 100% [98%–100%] |
| `undeclared-field` | contract | 179 | 179 | 0 | 0 | 100% [98%–100%] | 100% [98%–100%] |
| `inconsistent-balance` | business | 10 | 9 | 1 | 0 | 90.0% [60%–98%] | 100% [70%–100%] |
| `broken-pagination` | business | 137 | 137 | 0 | 0 | 100% [97%–100%] | 100% [97%–100%] |
| `consent-not-enforced` | business | 3 | 3 | 0 | 0 | 100% [44%–100%] | 100% [44%–100%] |
| `transaction-outside-window` | business | 146 | 146 | 0 | 0 | 100% [97%–100%] | 100% [97%–100%] |

### By rule

| Rule | Layer | Flagged | Hits on its targets | Precision |
|---|---|---|---|---|
| `schema.body` | contract | 329 | 329 | 100% [99%–100%] |
| `schema.header` | contract | 175 | 175 | 100% [98%–100%] |
| `interaction-id.echo` | contract | 184 | 184 | 100% [98%–100%] |
| `strict.undeclared-field` | contract | 179 | 179 | 100% [98%–100%] |
| `balance.matches-transactions` | business | 9 | 9 | 100% [70%–100%] |
| `pagination.duplicate-item` | business | 91 | 91 | 100% [96%–100%] |
| `pagination.missing-next` | business | 40 | 40 | 100% [91%–100%] |
| `pagination.total-records` | business | 6 | 6 | 100% [61%–100%] |
| `consent.enforced` | business | 3 | 3 | 100% [44%–100%] |
| `transactions.within-window` | business | 146 | 146 | 100% [97%–100%] |

### Who found what

Exchanges per label (rows) and rule that fired (columns).

| Label | `balance.matches-transactions` | `consent.enforced` | `interaction-id.echo` | `pagination.duplicate-item` | `pagination.missing-next` | `pagination.total-records` | `schema.body` | `schema.header` | `strict.undeclared-field` | `transactions.within-window` |
|---|---|---|---|---|---|---|---|---|---|---|
| `clean` |  |  |  |  |  |  |  |  |  |  |
| `missing-required-field` |  |  |  |  |  |  | 176 |  |  |  |
| `invalid-format` |  |  |  |  |  |  | 153 |  |  |  |
| `missing-interaction-id` |  |  | 184 |  |  |  |  | 175 |  |  |
| `undeclared-field` |  |  |  |  |  |  |  |  | 179 |  |
| `inconsistent-balance` | 9 |  |  |  |  |  |  |  |  |  |
| `broken-pagination` |  |  |  | 91 | 40 | 6 |  |  |  |  |
| `consent-not-enforced` |  | 3 |  |  |  |  |  |  |  |  |
| `transaction-outside-window` |  |  |  |  |  |  |  |  |  | 146 |

### Gate

All invariants hold: precision 1 in both layers, conditional recall 1 in every class, no inconclusive contract check.

