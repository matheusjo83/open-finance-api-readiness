# Reference detection report (2026-11-23)

A snapshot of `make campaign SEEDS=3 CUSTOMERS=50 REPORT_NAME=reference-20261123`, taken on
2026-11-23. It covers three runs, defect seeds 1 to 3, with the default rates and 50 synthetic
customers. The numbers hold for that day only ([report.md](report.md#campaigns-and-reproducibility)).
How to read them: [report.md](report.md). The earlier snapshots (v1.0.0 of 2026-11-18, v1.1.0 of
2026-11-19) are in the history of this file.

## Reading

- **Precision.** The contract and business layers flagged nothing clean, and nothing from the
  other layer.
- **Contract layer.** It found all 1,315 contract defects. Two of the classes are invisible to
  plain schema validation:
  - `undeclared-field` (332): only `strict.undeclared-field` sees it;
  - `missing-interaction-id` (426): `schema.header` sees 235, the Accounts 2xx responses. The
    other 191 (Consents, and the 401s and 403s) only `interaction-id.echo` finds.
- **Business layer.** Conditional recall is 100 % (963 of 963 conclusive). Strict recall is
  98.2 %, up from 90.8 % on 2026-11-19, because 18 defects were inconclusive instead of 89.
- **The second consent read (decision 33).** Each consent is now read twice before use, the second
  time with a renewed client-credentials token, so a draw of its own:
  - `consent-not-enforced`: 10 inconclusive of 661, down from 81. Strict recall went from 87.7 %
    to 98.5 %. Each read fails the contract layer in about 12 % of cases, so both fail in about
    1.5 %, which is what the campaign shows.
  - The cost: 1,053 more exchanges (8,532 against 7,479), the second reads: 7 per customer and
    one in each run's lifecycle.
- **Where the inconclusive checks are:**
  - `inconsistent-balance`: 8 of 43. No usable history for the account.
  - `consent-not-enforced`: 10 of 661. Both reads of the consent failed the contract layer.

## Report

Runs: `reference-20261123-seed1`, `reference-20261123-seed2`, `reference-20261123-seed3`. Exchanges: 8532. Intervals: 95 % Wilson.

### By layer

| Layer | Precision | Recall (strict) | Recall (conditional) | Detected | Inconclusive | Positives |
|---|---|---|---|---|---|---|
| contract | 100% [100%–100%] | 100% [100%–100%] | 100% [100%–100%] | 1315 | 0 | 1315 |
| business | 100% [100%–100%] | 98.2% [97%–99%] | 100% [100%–100%] | 963 | 18 | 981 |

### By defect class

| Class | Layer | Positives | Detected | Inconclusive | Missed | Recall (strict) | Recall (conditional) |
|---|---|---|---|---|---|---|---|
| `missing-required-field` | contract | 291 | 291 | 0 | 0 | 100% [99%–100%] | 100% [99%–100%] |
| `invalid-format` | contract | 266 | 266 | 0 | 0 | 100% [99%–100%] | 100% [99%–100%] |
| `missing-interaction-id` | contract | 426 | 426 | 0 | 0 | 100% [99%–100%] | 100% [99%–100%] |
| `undeclared-field` | contract | 332 | 332 | 0 | 0 | 100% [99%–100%] | 100% [99%–100%] |
| `inconsistent-balance` | business | 43 | 35 | 8 | 0 | 81.4% [67%–90%] | 100% [90%–100%] |
| `broken-pagination` | business | 159 | 159 | 0 | 0 | 100% [98%–100%] | 100% [98%–100%] |
| `consent-not-enforced` | business | 661 | 651 | 10 | 0 | 98.5% [97%–99%] | 100% [99%–100%] |
| `transaction-outside-window` | business | 118 | 118 | 0 | 0 | 100% [97%–100%] | 100% [97%–100%] |

### By rule

| Rule | Layer | Flagged | Hits on its targets | Precision |
|---|---|---|---|---|
| `schema.body` | contract | 557 | 557 | 100% [99%–100%] |
| `schema.header` | contract | 235 | 235 | 100% [98%–100%] |
| `interaction-id.echo` | contract | 426 | 426 | 100% [99%–100%] |
| `strict.undeclared-field` | contract | 332 | 332 | 100% [99%–100%] |
| `balance.matches-transactions` | business | 35 | 35 | 100% [90%–100%] |
| `pagination.duplicate-item` | business | 105 | 105 | 100% [96%–100%] |
| `pagination.missing-next` | business | 44 | 44 | 100% [92%–100%] |
| `pagination.total-records` | business | 10 | 10 | 100% [72%–100%] |
| `consent.enforced` | business | 651 | 651 | 100% [99%–100%] |
| `transactions.within-window` | business | 118 | 118 | 100% [97%–100%] |

### Who found what

Exchanges per label (rows) and rule that fired (columns).

| Label | `balance.matches-transactions` | `consent.enforced` | `interaction-id.echo` | `pagination.duplicate-item` | `pagination.missing-next` | `pagination.total-records` | `schema.body` | `schema.header` | `strict.undeclared-field` | `transactions.within-window` |
|---|---|---|---|---|---|---|---|---|---|---|
| `clean` |  |  |  |  |  |  |  |  |  |  |
| `missing-required-field` |  |  |  |  |  |  | 291 |  |  |  |
| `invalid-format` |  |  |  |  |  |  | 266 |  |  |  |
| `missing-interaction-id` |  |  | 426 |  |  |  |  | 235 |  |  |
| `undeclared-field` |  |  |  |  |  |  |  |  | 332 |  |
| `inconsistent-balance` | 35 |  |  |  |  |  |  |  |  |  |
| `broken-pagination` |  |  |  | 105 | 44 | 10 |  |  |  |  |
| `consent-not-enforced` |  | 651 |  |  |  |  |  |  |  |  |
| `transaction-outside-window` |  |  |  |  |  |  |  |  |  | 118 |

### Gate

All invariants hold: precision 1 in both layers, conditional recall 1 in every class, no inconclusive contract check.
