# Reference detection report (2026-11-19)

A snapshot of `make campaign SEEDS=3 CUSTOMERS=50 REPORT_NAME=reference-20261119`, taken on
2026-11-19. It covers three runs, defect seeds 1 to 3, with the default rates and 50 synthetic
customers. The numbers hold for that day only ([report.md](report.md#campaigns-and-reproducibility)).
How to read them: [report.md](report.md). The v1.0.0 snapshot (2026-11-18) is in the history of
this file.

## Reading

- **Precision.** The contract and business layers flagged nothing clean, and nothing from the
  other layer.
- **Contract layer.** It found all 1,126 contract defects. Two of the classes are invisible to
  plain schema validation:
  - `undeclared-field` (282): only `strict.undeclared-field` sees it;
  - `missing-interaction-id` (360): `schema.header` sees 223, the Accounts 2xx responses. The
    other 137 (Consents, and the 401s and 403s that the walk now makes) only
    `interaction-id.echo` finds.
- **Business layer.** Conditional recall is 100 % (882 of 882 conclusive). Strict recall is
  90.8 %, because 89 defects were inconclusive.
- **More positives than in v1.0.0.** The walk now uses every customer's non-authorised consents,
  and reads each balance with two more consents (decision 31):
  - `consent-not-enforced`: 661 positives, up from 3. The interval narrowed from 44–100 % to
    99–100 % (conditional).
  - `inconsistent-balance`: 40 positives, up from 10. The interval went from 70–100 % to
    90–100 % (conditional).
- **Where the inconclusive checks are:**
  - `consent-not-enforced`: 81 of 661, or 12 %. The consent is read once before it is used. When
    that read fails the contract layer, the rule cannot know the consent's state.
  - `inconsistent-balance`: 7 of 40. No usable history for the account.
  - `broken-pagination`: 1 of 144.
- **A finding of the campaign.** On the first pass, the gate failed, which is what it is for. A
  page lost its `next` link, and the only later page, the walk's probe, then failed the contract
  layer, so the rule said "pass" without evidence. `pagination.missing-next` is now inconclusive
  in that case. The three runs were checked again with the fixed rule, and a regression test
  covers it (`test_missing_next_is_inconclusive_when_the_later_pages_were_left_out`).

## Report

Runs: `reference-20261119-seed1`, `reference-20261119-seed2`, `reference-20261119-seed3`. Exchanges: 7479. Intervals: 95 % Wilson.

### By layer

| Layer | Precision | Recall (strict) | Recall (conditional) | Detected | Inconclusive | Positives |
|---|---|---|---|---|---|---|
| contract | 100% [100%–100%] | 100% [100%–100%] | 100% [100%–100%] | 1126 | 0 | 1126 |
| business | 100% [100%–100%] | 90.8% [89%–92%] | 100% [100%–100%] | 882 | 89 | 971 |

### By defect class

| Class | Layer | Positives | Detected | Inconclusive | Missed | Recall (strict) | Recall (conditional) |
|---|---|---|---|---|---|---|---|
| `missing-required-field` | contract | 245 | 245 | 0 | 0 | 100% [98%–100%] | 100% [98%–100%] |
| `invalid-format` | contract | 239 | 239 | 0 | 0 | 100% [98%–100%] | 100% [98%–100%] |
| `missing-interaction-id` | contract | 360 | 360 | 0 | 0 | 100% [99%–100%] | 100% [99%–100%] |
| `undeclared-field` | contract | 282 | 282 | 0 | 0 | 100% [99%–100%] | 100% [99%–100%] |
| `inconsistent-balance` | business | 40 | 33 | 7 | 0 | 82.5% [68%–91%] | 100% [90%–100%] |
| `broken-pagination` | business | 144 | 143 | 1 | 0 | 99.3% [96%–100%] | 100% [97%–100%] |
| `consent-not-enforced` | business | 661 | 580 | 81 | 0 | 87.7% [85%–90%] | 100% [99%–100%] |
| `transaction-outside-window` | business | 126 | 126 | 0 | 0 | 100% [97%–100%] | 100% [97%–100%] |

### By rule

| Rule | Layer | Flagged | Hits on its targets | Precision |
|---|---|---|---|---|
| `schema.body` | contract | 484 | 484 | 100% [99%–100%] |
| `schema.header` | contract | 223 | 223 | 100% [98%–100%] |
| `interaction-id.echo` | contract | 360 | 360 | 100% [99%–100%] |
| `strict.undeclared-field` | contract | 282 | 282 | 100% [99%–100%] |
| `balance.matches-transactions` | business | 33 | 33 | 100% [90%–100%] |
| `pagination.duplicate-item` | business | 91 | 91 | 100% [96%–100%] |
| `pagination.missing-next` | business | 42 | 42 | 100% [92%–100%] |
| `pagination.total-records` | business | 10 | 10 | 100% [72%–100%] |
| `consent.enforced` | business | 580 | 580 | 100% [99%–100%] |
| `transactions.within-window` | business | 126 | 126 | 100% [97%–100%] |

### Who found what

Exchanges per label (rows) and rule that fired (columns).

| Label | `balance.matches-transactions` | `consent.enforced` | `interaction-id.echo` | `pagination.duplicate-item` | `pagination.missing-next` | `pagination.total-records` | `schema.body` | `schema.header` | `strict.undeclared-field` | `transactions.within-window` |
|---|---|---|---|---|---|---|---|---|---|---|
| `clean` |  |  |  |  |  |  |  |  |  |  |
| `missing-required-field` |  |  |  |  |  |  | 245 |  |  |  |
| `invalid-format` |  |  |  |  |  |  | 239 |  |  |  |
| `missing-interaction-id` |  |  | 360 |  |  |  |  | 223 |  |  |
| `undeclared-field` |  |  |  |  |  |  |  |  | 282 |  |
| `inconsistent-balance` | 33 |  |  |  |  |  |  |  |  |  |
| `broken-pagination` |  |  |  | 91 | 42 | 10 |  |  |  |  |
| `consent-not-enforced` |  | 580 |  |  |  |  |  |  |  |  |
| `transaction-outside-window` |  |  |  |  |  |  |  |  |  | 126 |

### Gate

All invariants hold: precision 1 in both layers, conditional recall 1 in every class, no inconclusive contract check.

