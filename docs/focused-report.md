# Focused detection report (2026-11-24)

A snapshot of `make focused-campaign SEEDS=3 CUSTOMERS=50 REPORT_NAME=focused-20261124`, taken
on 2026-11-24, the same day and walk as the [reference campaign](detection-report.md). It
covers three runs, defect seeds 1 to 3, with 50 synthetic customers and the focused rates: the
contract classes off, `inconsistent-balance` 0.3, `broken-pagination` 0.25,
`consent-not-enforced` 0.1, `transaction-outside-window` 0.05, `broken-extension-history` 0.15
and `extension-not-reflected` 0.15, where each applies
([report.md](report.md#campaigns-and-reproducibility), decisions 37 and 38). The snapshot of
2026-11-23, before the renewal classes, is in the history of this file.

The mix is artificial. These figures say how well each business rule finds its class when it
sees it many times, not how the pipeline does on realistic traffic: that is the reference
campaign.

## Reading

- **Precision.** The business layer flagged 1,316 exchanges, all of them its own defects, and
  nothing clean. No contract defect was injected, so the contract layer has nothing to measure.
- **Conditional recall is 100 % in every class.** Exchanges flagged per rule, with the 95 %
  interval of its precision:

  | Rule | Reference (2026-11-24) | Focused |
  |---|---|---|
  | `balance.matches-transactions` | 41, 91–100 % | 238, 98–100 % |
  | `pagination.duplicate-item` | 95, 96–100 % | 464, 99–100 % |
  | `pagination.missing-next` | 36, 90–100 % | 205, 98–100 % |
  | `pagination.total-records` | 10, 72–100 % | 49, 93–100 % |
  | `consent.enforced` | 651, 99–100 % | 151, 98–100 % |
  | `transactions.within-window` | 132, 97–100 % | 131, 97–100 % |
  | `extensions.history-consistent` | 18, 82–100 % | 40, 91–100 % |
  | `consent.extension-reflected` | 15, 80–100 % | 38, 91–100 % |

  Recall per class, with its interval, is in the report below.
- **The renewal classes have the fewest cases.** Each customer has one renewed consent: two
  history reads and two consent reads after the renewals. At 0.15, that gives about 40 cases
  each over three runs. More would need more renewals in the walk, or rates the single draw
  does not allow (the rates add up to 1).
- **`pagination.total-records` is still narrow.** The `wrong-total` variant applies only to the
  accounts list with `meta.totalRecords`, one page per consent, and is drawn among the
  variants that apply.
- **Where the inconclusive checks are:** `inconsistent-balance`, 46 of 284 (16 %). Nothing is
  left out by the contract layer here; the balance needs a complete history of the account
  without repeated items, and at 0.25 `broken-pagination` breaks many of them. Strict recall
  (83.8 %) measures that interaction, not the rule.
- **Memory.** No executor was OOMKilled in either campaign of the day, with the 768m overhead
  of the business-rules job and the report (decisions 35 and 37).

## Report

Runs: `focused-20261124-seed1`, `focused-20261124-seed2`, `focused-20261124-seed3`. Exchanges: 10182. Intervals: 95 % Wilson.

### By layer

| Layer | Precision | Recall (strict) | Recall (conditional) | Detected | Inconclusive | Positives |
|---|---|---|---|---|---|---|
| contract | — | — | — | 0 | 0 | 0 |
| business | 100% [100%–100%] | 96.6% [96%–97%] | 100% [100%–100%] | 1316 | 46 | 1362 |

### By defect class

| Class | Layer | Positives | Detected | Inconclusive | Missed | Recall (strict) | Recall (conditional) |
|---|---|---|---|---|---|---|---|
| `missing-required-field` | contract | 0 | 0 | 0 | 0 | — | — |
| `invalid-format` | contract | 0 | 0 | 0 | 0 | — | — |
| `missing-interaction-id` | contract | 0 | 0 | 0 | 0 | — | — |
| `undeclared-field` | contract | 0 | 0 | 0 | 0 | — | — |
| `inconsistent-balance` | business | 284 | 238 | 46 | 0 | 83.8% [79%–88%] | 100% [98%–100%] |
| `broken-pagination` | business | 718 | 718 | 0 | 0 | 100% [99%–100%] | 100% [99%–100%] |
| `consent-not-enforced` | business | 151 | 151 | 0 | 0 | 100% [98%–100%] | 100% [98%–100%] |
| `transaction-outside-window` | business | 131 | 131 | 0 | 0 | 100% [97%–100%] | 100% [97%–100%] |
| `broken-extension-history` | business | 40 | 40 | 0 | 0 | 100% [91%–100%] | 100% [91%–100%] |
| `extension-not-reflected` | business | 38 | 38 | 0 | 0 | 100% [91%–100%] | 100% [91%–100%] |

### By rule

| Rule | Layer | Flagged | Hits on its targets | Precision |
|---|---|---|---|---|
| `balance.matches-transactions` | business | 238 | 238 | 100% [98%–100%] |
| `pagination.duplicate-item` | business | 464 | 464 | 100% [99%–100%] |
| `pagination.missing-next` | business | 205 | 205 | 100% [98%–100%] |
| `pagination.total-records` | business | 49 | 49 | 100% [93%–100%] |
| `consent.enforced` | business | 151 | 151 | 100% [98%–100%] |
| `transactions.within-window` | business | 131 | 131 | 100% [97%–100%] |
| `extensions.history-consistent` | business | 40 | 40 | 100% [91%–100%] |
| `consent.extension-reflected` | business | 38 | 38 | 100% [91%–100%] |

### Who found what

Exchanges per label (rows) and rule that fired (columns).

| Label | `balance.matches-transactions` | `consent.enforced` | `consent.extension-reflected` | `extensions.history-consistent` | `pagination.duplicate-item` | `pagination.missing-next` | `pagination.total-records` | `transactions.within-window` |
|---|---|---|---|---|---|---|---|---|
| `clean` |  |  |  |  |  |  |  |  |
| `inconsistent-balance` | 238 |  |  |  |  |  |  |  |
| `broken-pagination` |  |  |  |  | 464 | 205 | 49 |  |
| `consent-not-enforced` |  | 151 |  |  |  |  |  |  |
| `transaction-outside-window` |  |  |  |  |  |  |  | 131 |
| `broken-extension-history` |  |  |  | 40 |  |  |  |  |
| `extension-not-reflected` |  |  | 38 |  |  |  |  |  |

### Gate

All invariants hold: precision 1 in both layers, conditional recall 1 in every class, no inconclusive contract check.
