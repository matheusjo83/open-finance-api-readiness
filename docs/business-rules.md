# Business rules job

`jobs/business` is the business layer: a PySpark job that reads one validation run from the
object store and checks the responses against the domain rules the
[transmitter guarantees](transmitter.md#invariants), which the proxy's business defects break.
It writes bronze, silver and gold Iceberg tables (`lake.ofb.*`). It never reads the labels: the
comparison with the ground truth is the [detection report](report.md)'s.

## Input and gating

The job reads two inputs written by the [contract validator](validator.md), both under
`s3a://lakehouse/`:

- the exchange log, `exchanges/run=<run_id>/`;
- the contract results, `contract/run=<run_id>/`.

Spark reads them through the S3A connector (decision 22).

The business layer only looks at 2xx exchanges the contract layer did not fail (verdict `pass`
or `warn`). A response that breaks the spec is the contract layer's to report. Parsing it for
business rules would mix the two layers (decision 24). When a rule's input was left out this
way, or is incomplete, the rule reports `inconclusive` instead of guessing.

## Rules

| Rule | Evaluated on | Finding when | Inconclusive when |
|---|---|---|---|
| `balance.matches-transactions` | each balances response | `availableAmount` ≠ the signed sum (credits minus debits) of the account's `TRANSACAO_EFETIVADA` transactions | no complete transaction history for the account in the same walk (from any consent: the balance belongs to the account): a page missing, excluded, or with a repeated item |
| `pagination.duplicate-item` | each page of a listing | an item id (`transactionId`, `accountId`) already appeared earlier in the listing | — |
| `pagination.missing-next` | each page | no `links.next`, although `meta.totalPages` is larger or a later page of the listing has items | a full page without `links.next` whose later pages were all left out by the contract layer |
| `pagination.total-records` | each page with `meta.totalRecords` | it differs from the items of the whole listing | the listing is incomplete |
| `consent.enforced` | each 200 from Accounts | the consent's last read before the call (`GET /consents/{id}`) that passed the contract layer is not `AUTHORISED`, or its `expirationDateTime` has passed | no earlier read of the consent passed the contract layer |
| `transactions.within-window` | each transactions page | a `transactionDateTime` falls outside `fromBookingDate..toBookingDate` | the query has no window |
| `extensions.history-consistent` | each renewal history page with items | `requestDateTime` increases down the list, a `previousExpirationDateTime` is not the next renewal's `expirationDateTime`, or the newest renewal's `expirationDateTime` is not what the consent's last 201 renewal asked for | no finding, and that last renewal's response was left out by the contract layer |
| `consent.extension-reflected` | each consent read after a 201 renewal of the consent | `expirationDateTime` is not what the last renewal asked for (the walk's own request body) | that renewal's response was left out by the contract layer: the renewal may not have taken effect |

The four defect classes map to these rules:

- `inconsistent-balance`: `balance.matches-transactions`;
- `broken-pagination`: `duplicate-item`, `missing-next` and `wrong-total` each map to their
  `pagination.*` rule;
- `consent-not-enforced`: `consent.enforced`;
- `transaction-outside-window`: `transactions.within-window`;
- `broken-extension-history`: `extensions.history-consistent`;
- `extension-not-reflected`: `consent.extension-reflected`.

A *listing* is the set of pages of one request. Its pages share the walk, the consent, the path,
the page size and the query apart from `page`. A listing is complete when it has pages 1..n and
the last one has no next link. The walk orders the calls with `seq` and reads every consent
before using it. It also probes the next page after a full page with no next link
([validator.md](validator.md#the-walk)).

### What the default rates show

With the default catalogue (about 25 % of the responses broken), some checks are inconclusive:

- **Balances.** A balance needs the account's whole history. The walk also asks for it in one
  page of 1000, and the rule uses the usable history with the largest pages. So one broken
  response, not one of five, makes it inconclusive. That brought inconclusive balances from 7 of
  9 to 3 of 9 on the default-rate test run.
- **Consents.** When the one read of a customer's consent fails the contract layer, every
  Accounts call with that consent is inconclusive.

These are limits of a layered pipeline, not errors. The [report](report.md) counts them apart
from detections and misses (strict and conditional recall).

## Tables

All in the Iceberg catalog `lake`, namespace `ofb`, partitioned by `run_id`. Each run replaces
its own partition (`overwritePartitions`), so a rerun does not duplicate rows.

| Layer | Table | One row per |
|---|---|---|
| bronze | `exchanges` | exchange, as the validator logged it (`ts` as a timestamp) |
| bronze | `contract_results` | contract result |
| silver | `consent_reads` | consent read that passed the contract layer: `consent_id`, `consent_status`, `expiration_ts` |
| silver | `balances` | balances response: `account_id`, `available_amount` (decimal) |
| silver | `listing_pages` | page: `listing`, `page`, `page_size`, `has_next`, `total_records`, `total_pages`, `item_count`, booking window |
| silver | `listing_items` | item of a page: `item_id`, `credit_debit_type`, `payment_type`, `amount`, `transaction_date_time` |
| silver | `extension_items` | renewal in a history page that passed the contract layer: `consent_id`, `item_index`, `expiration_ts`, `previous_ts`, `request_ts` |
| silver | `renewals` | renewal answered 201: `consent_id`, `requested_ts` (from the walk's request body), `usable` (its response passed the contract layer) |
| gold | `business_checks` | rule evaluated on an exchange: `rule`, `outcome` (`pass`, `finding`, `inconclusive`), `pointer`, `detail` |

Every table carries `run_id`, `walk_id`, `interaction_id` (the join key with the labels and the
contract results) and, where it applies, `seq` and `operation`. A finding's `pointer` is a JSON
pointer into the body, or `status` (consent not enforced). Amounts are `decimal(20,4)`.

## Running it

```bash
make jobs-test        # local Spark over runs exported in process (no cluster)
make business-rules   # the job over the proxy's current run (or RUN_ID=...), as a SparkApplication
```

`make jobs-test` first exports eight runs with the validator's test harness
(`services/validator/tests/export_runs.py`):

- `clean`;
- `default`;
- one per business class at rate 1.

It writes them to `jobs/tests/.runs/` with the object store's layout. The tests then run the
job's transformations with a local Spark on Python 3.10, the Spark image's Python. They need
Java 17.
