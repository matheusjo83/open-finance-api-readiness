# Focused detection report (2026-11-23)

A snapshot of `make focused-campaign SEEDS=3 CUSTOMERS=50 REPORT_NAME=focused-20261123`, taken
on 2026-11-23, the same day and walk as the [reference campaign](detection-report.md). It
covers three runs, defect seeds 1 to 3, with 50 synthetic customers and the focused rates: the
contract classes off, `inconsistent-balance` 0.4, `broken-pagination` 0.3,
`consent-not-enforced` 0.2 and `transaction-outside-window` 0.1, where each applies
([report.md](report.md#campaigns-and-reproducibility), decision 37).

The mix is artificial. These figures say how well each business rule finds its class when it
sees it many times, not how the pipeline does on realistic traffic: that is the reference
campaign.

## Reading

- **Precision.** The business layer flagged 1,620 exchanges, all of them its own defects, and
  nothing clean. No contract defect was injected, so the contract layer has nothing to measure.
- **Conditional recall is 100 % in every class**, and each rule now has hundreds of cases.
  Exchanges flagged per rule, with the 95 % interval of its precision:

  | Rule | Reference (2026-11-23) | Focused |
  |---|---|---|
  | `balance.matches-transactions` | 35, 90–100 % | 286, 99–100 % |
  | `pagination.duplicate-item` | 105, 96–100 % | 534, 99–100 % |
  | `pagination.missing-next` | 44, 92–100 % | 243, 98–100 % |
  | `pagination.total-records` | 10, 72–100 % | 55, 93–100 % |
  | `consent.enforced` | 651, 99–100 % | 269, 99–100 % |
  | `transactions.within-window` | 118, 97–100 % | 233, 98–100 % |

  Recall per class, with its interval, is in the report below: `inconsistent-balance` goes
  from 90–100 % to 99–100 % (conditional), `broken-pagination` from 98–100 % to 100–100 %.
- **`pagination.total-records` is still the narrowest.** The `wrong-total` variant applies only
  to the accounts list with `meta.totalRecords`, one page per consent, and is drawn among the
  variants that apply, so it gets the fewest cases at any rate.
- **Where the inconclusive checks are:** `inconsistent-balance`, 89 of 375 (24 %). Nothing is
  left out by the contract layer here; the balance needs a complete history of the account
  without repeated items, and at 0.3 `broken-pagination` breaks many of them. Strict recall
  (76.3 %) measures that interaction, not the rule.
- **Memory.** The business-rules job lost executors to the OOM killer in seeds 2 and 3 (Spark
  retried; the tables are the same). Its memory overhead is now 768m, as for the report; a rerun
  of seed 2 peaked at 1273 MiB of 1792 MiB, against 1311 MiB of 1433 MiB, and the report over
  the three runs was the same (decision 37).

## Report

Runs: `focused-20261123-seed1`, `focused-20261123-seed2`, `focused-20261123-seed3`. Exchanges: 8532. Intervals: 95 % Wilson.

### By layer

| Layer | Precision | Recall (strict) | Recall (conditional) | Detected | Inconclusive | Positives |
|---|---|---|---|---|---|---|
| contract | — | — | — | 0 | 0 | 0 |
| business | 100% [100%–100%] | 94.8% [94%–96%] | 100% [100%–100%] | 1620 | 89 | 1709 |

### By defect class

| Class | Layer | Positives | Detected | Inconclusive | Missed | Recall (strict) | Recall (conditional) |
|---|---|---|---|---|---|---|---|
| `missing-required-field` | contract | 0 | 0 | 0 | 0 | — | — |
| `invalid-format` | contract | 0 | 0 | 0 | 0 | — | — |
| `missing-interaction-id` | contract | 0 | 0 | 0 | 0 | — | — |
| `undeclared-field` | contract | 0 | 0 | 0 | 0 | — | — |
| `inconsistent-balance` | business | 375 | 286 | 89 | 0 | 76.3% [72%–80%] | 100% [99%–100%] |
| `broken-pagination` | business | 832 | 832 | 0 | 0 | 100% [100%–100%] | 100% [100%–100%] |
| `consent-not-enforced` | business | 269 | 269 | 0 | 0 | 100% [99%–100%] | 100% [99%–100%] |
| `transaction-outside-window` | business | 233 | 233 | 0 | 0 | 100% [98%–100%] | 100% [98%–100%] |

### By rule

| Rule | Layer | Flagged | Hits on its targets | Precision |
|---|---|---|---|---|
| `balance.matches-transactions` | business | 286 | 286 | 100% [99%–100%] |
| `pagination.duplicate-item` | business | 534 | 534 | 100% [99%–100%] |
| `pagination.missing-next` | business | 243 | 243 | 100% [98%–100%] |
| `pagination.total-records` | business | 55 | 55 | 100% [93%–100%] |
| `consent.enforced` | business | 269 | 269 | 100% [99%–100%] |
| `transactions.within-window` | business | 233 | 233 | 100% [98%–100%] |

### Who found what

Exchanges per label (rows) and rule that fired (columns).

| Label | `balance.matches-transactions` | `consent.enforced` | `pagination.duplicate-item` | `pagination.missing-next` | `pagination.total-records` | `transactions.within-window` |
|---|---|---|---|---|---|---|
| `clean` |  |  |  |  |  |  |
| `inconsistent-balance` | 286 |  |  |  |  |  |
| `broken-pagination` |  |  | 534 | 243 | 55 |  |
| `consent-not-enforced` |  | 269 |  |  |  |  |
| `transaction-outside-window` |  |  |  |  |  | 233 |

### Gate

All invariants hold: precision 1 in both layers, conditional recall 1 in every class, no inconclusive contract check.
