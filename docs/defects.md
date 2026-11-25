# Defect proxy and catalogue

`services/proxy` sits between the validators and the [synthetic transmitter](transmitter.md):
`http://proxy.ofb.svc` forwards every request to `http://transmitter.ofb.svc` and may break the
response with one catalogued defect. It writes a label for every Open Finance exchange, clean or
not, so precision and recall can be computed per class and per layer (item 5).

## Catalogue

Contract defects break the pinned specification. Business defects keep the response valid
against the specification and break one of the transmitter's
[invariants](transmitter.md#invariants), so each layer is measured on its own.
`services/proxy/tests/test_proxy.py` checks both halves for every class.

| Class | Layer | Applies to | What changes | Default rate |
|---|---|---|---|---|
| `missing-required-field` | contract | 2xx with a body (not overdraft-limits) | A required field of `data` (or of one list item) is removed | 0.05 |
| `invalid-format` | contract | 2xx with an amount or a `*DateTime` under `data` | `"123.45"` becomes `"123,45"` or `"123.4"`; `2026-11-16T10:00:00Z` becomes `2026-11-16 10:00:00` | 0.05 |
| `missing-interaction-id` | contract | every Open Finance response | The `x-fapi-interaction-id` response header is dropped | 0.05 |
| `undeclared-field` | contract | 2xx with a body | `internalReference` is added to `data` or to one item | 0.05 |
| `inconsistent-balance` | business | balances | `availableAmount` shifted by ±0.01 to ±499.99 | 0.05 |
| `broken-pagination` | business | accounts list, transactions | One of: last item replaced by a copy of the first; `links.next` removed while more pages exist; `meta.totalRecords` increased | 0.05 |
| `consent-not-enforced` | business | accounts endpoints answering 401 for a seeded expired, revoked or awaiting consent | The 401 is replaced by the 200 the customer's authorised consent gets | 0.5 |
| `transaction-outside-window` | business | transactions with at least one item | One `transactionDateTime` moved 1 to 30 days before or after the requested window | 0.05 |
| `broken-extension-history` | business | renewal histories with at least two renewals | One of: the list reversed (oldest first); a `previousExpirationDateTime` other than the oldest one's moved 1 to 30 days | 0.05 |
| `extension-not-reflected` | business | consent reads of a renewed consent (the proxy asks the transmitter for its history) | `expirationDateTime` set back to the one before the last renewal | 0.05 |

Two of the contract defects are not caught by validating against the schema alone, and the
measurement should show it:

- `undeclared-field`: no schema in the pinned specs sets `additionalProperties: false`, so an
  extra field is valid. Only a stricter contract check (fields not in the schema) finds it.
- `missing-interaction-id` on Consents: the Consents 3.3.1 spec does not mark the response
  header as required (Accounts 2.4.2 does), although its description says the transmitter must
  echo it. On Consents, and on Accounts error responses, only an explicit echo rule finds it
  ([spec-findings.md](spec-findings.md)).

The contract validator ([validator.md](validator.md)) has both rules.

`consent-not-enforced` has a higher rate because it only applies to the few calls made with a
non-authorised consent. The two renewal classes come last in the catalogue, so adding them left
the other classes' bands of the draw, and so their decisions, as they were. The defaults add up
to 0.95.

## Selection

The decision depends only on the seed (`DEFECT_SEED`) and the request: method, path, sorted query
and bearer token. The interaction id is left out, so a retry gets the same answer, and the
validators cannot influence it. One uniform draw from a SHA-256 of that key walks the applicable
classes in catalogue order, adding up their rates. At most one defect per response, and each
class is picked with probability equal to its rate where it applies. The rates must add up to
at most 1. A second hash, with the class name added, picks the field or variant.

`DEFECT_RATES` overrides rates as JSON, for example `{"invalid-format": 0.1, "consent-not-enforced": 0}`.

## Labels

Each Open Finance exchange (`/open-banking/*`) becomes one JSON line in
`s3://lakehouse/labels/run=<run_id>/part-NNNNN-<pod>.jsonl`. The `/mock/*` calls pass through
unlabelled. The proxy writes a part every 5 seconds (`LABELS_FLUSH_SECONDS`), every 500 records,
on `POST /_proxy/flush` and at shutdown. A failed write keeps the records for the next one.

| Field | Meaning |
|---|---|
| `run_id` | `RUN_ID`, or `<UTC start time>-<8 hex>` when not set; `GET /_proxy/run` returns it |
| `ts` | When the proxy answered (UTC, milliseconds) |
| `interaction_id` | The `x-fapi-interaction-id` the client sent: the join key with the validators' results |
| `method`, `path`, `route`, `query`, `consent` | The request (`route` such as `accounts.balances`; `consent` is the bearer token) |
| `upstream_status`, `status` | What the transmitter answered and what the client got |
| `seed` | `DEFECT_SEED` |
| `defect_class`, `defect_layer` | The class and its layer, or null for a clean response |
| `defect_variant`, `defect_target`, `defect_detail` | How it was broken: the variant, a JSON pointer (or `header:<name>`, `status`) and a before/after note |

## Settings

| Variable | Default | |
|---|---|---|
| `UPSTREAM` | `http://transmitter.ofb.svc` | The transmitter |
| `DEFECT_SEED` | `42` | Seed of the selection |
| `DEFECT_RATES` | the defaults above | JSON overrides |
| `RUN_ID` | generated | Label prefix |
| `S3_ENDPOINT`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `LABELS_BUCKET`, `AWS_REGION` | SeaweedFS, `lakehouse` | Label store (`labels-credentials` Secret) |
| `LABELS_FLUSH_SECONDS` | `5` | Write interval |

The proxy also writes the server's PCM report of every exchange, next to the labels
(`s3://lakehouse/pcm/server/run=<run_id>/`, [telemetry.md](telemetry.md)).

The proxy forwards the caller's `Host`, so the links the transmitter builds (`self`, `next`, …)
point back at the proxy and pagination stays behind it.

## Tests and checks

- `make proxy-test` runs the proxy over the real transmitter, in process, with an in-memory label
  store. It checks that:
  - the proxy is byte-for-byte transparent with all rates at zero;
  - every class does what the table says;
  - decisions repeat for the same seed and change with another one;
  - the rates hold over 20,000 draws;
  - the label sink survives a failed write.
- `make proxy-smoke` walks the seeded resources through the proxy in the cluster and reads the
  labels back from SeaweedFS. Every call must have its label (by interaction id), and at least
  one call must carry a defect.
