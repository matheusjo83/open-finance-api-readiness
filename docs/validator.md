# Contract validator

`services/validator` is the contract layer. It plays the receiving institution: it walks the
seeded resources through the [defect proxy](defects.md) (`http://proxy.ofb.svc`), checks every
Open Finance response against the pinned specifications, and writes one result per exchange to
the object store, next to the proxy's labels. It never reads the labels; the join is done after
the run (`make validator-smoke` now, the detection report in item 5).

Decision 19 in [decisions.md](decisions.md) explains why it is built on openapi-core instead of
Schemathesis.

## The walk

A fixed sequence over `GET /mock/seed`, the same for every run:

- per customer: its authorised consent (`GET /consents/{consentId}`), the accounts list (every
  page), and per account the detail, balances, overdraft limits, transactions over the last 12
  months (every page, following `links.next`, then again in one page of 1000 for the balance
  reconciliation) and the current transactions (every page);
- per customer, each of its other seeded consents (expired, revoked, without balances,
  awaiting) is read, then used on the accounts list and on every balance. That gives a 401 when
  the consent is not authorised, and a 403 for the balances of the consent without the
  permission;
- per customer, `BALANCE_CONSENTS` (default 2) new consents are created (201), authorised
  (`/mock`) and read, and each reads every balance again. A request of its own means a draw of
  its own, so the balance rule gets more independent cases;
- for the first customer: an unknown account (404), and a consent lifecycle: create, authorise,
  read, use, revoke (204), read again.

A consent is always read before it is used, so the business rules know its state
([business-rules.md](business-rules.md)). It is read twice, the second time with a renewed
client-credentials token. The proxy keys its draw on the bearer, so this second read gets a draw
of its own, and the state is still known when one of the two reads fails the contract layer
(decision 33). The read after the revocation, in the lifecycle, is made once: nothing uses the
consent after it. When a full page comes without `links.next`, the walk
asks for the next page number anyway, a careful client's probe. A dropped next link then
shows up as a later page with items.

It covers the 9 implemented operations. Every call carries a fresh `x-fapi-interaction-id`. That
id joins the result with the proxy's label. With 5 customers it makes about 270 calls;
with 50, about 2,400.

The defects the proxy draws depend on the requests (decision 17), and the walk is fixed, so two
runs over the same seed see the same defects. A defect can also change the walk. For example,
a removed `links.next` ends the pagination early.

## Rules

| Rule | Severity | What it checks |
|---|---|---|
| `schema.operation` | error | The method and path are an operation of the spec |
| `schema.status` | error | The status is declared for the operation (or `default`) |
| `schema.content-type` | error | The content type is declared for that status |
| `schema.header` | error | Required response headers are present and valid |
| `schema.body` | error | The body is valid against the response schema (one finding per schema error, with its JSON pointer) |
| `interaction-id.echo` | error | The `x-fapi-interaction-id` sent comes back unchanged |
| `strict.undeclared-field` | warning | Every body member is declared by the response schema |

The `schema.*` rules come from openapi-core (`iter_response_errors`) on the pinned files. The
other two cover what the specs leave out:

- **Echo.** Both specs say the transmitter must echo `x-fapi-interaction-id`, but the schemas
  require the header only on Accounts success responses. Consents never marks it as required, and
  Accounts error responses do not declare it ([spec-findings.md](spec-findings.md)). Without
  this rule, the proxy's `missing-interaction-id` would go unnoticed on those responses.
- **Strict.** No success schema sets `additionalProperties: false`, so schema validation accepts
  an extra field. The strict rule walks the body with the response schema and reports every member
  the schema does not declare. `allOf`/`oneOf`/`anyOf` declare the union of their branches. A
  schema that sets `additionalProperties` (false, true or a schema) decides for itself, and a
  free-form object (no `properties`) accepts anything. It is a warning: an extra field is not
  forbidden by the specs, only undocumented.

A result's `verdict` is `fail` with at least one error, `warn` with warnings only, `pass` otherwise.

Over the proxy's catalogue the contract layer is exact by construction. Every contract defect
trips the rule meant for it. Business defects keep the response valid and trip nothing. Clean
responses trip nothing. `make validator-test` and `make validator-smoke` both check this.

## Output

Two JSONL streams per run, under the proxy's run id (from `GET /_proxy/run`), so labels, results
and exchanges share `run=<id>`:

- `s3://lakehouse/contract/run=<run_id>/part-NNNNN-<walk_id>.jsonl`: one result per exchange;
- `s3://lakehouse/exchanges/run=<run_id>/part-NNNNN-<walk_id>.jsonl`: the exchange itself, the
  input of the business-rules job (item 4).

The walk id (8 hex characters) keeps two walks over the same proxy run apart. Without a proxy
(`TARGET` pointing straight at the transmitter), the run id is `RUN_ID`, or a generated one.

Results:

| Field | Meaning |
|---|---|
| `run_id`, `walk_id`, `ts` | The run, the walk, when the result was written (UTC, milliseconds) |
| `seq` | The call's position in the walk (1, 2, ...): the order the business rules rely on |
| `interaction_id` | The `x-fapi-interaction-id` sent: the join key with the labels |
| `method`, `path`, `query` | The request |
| `api`, `operation` | The spec (`accounts-2.4.2`) and its `operationId`, or null outside the spec |
| `status` | What the validator got |
| `verdict` | `pass`, `warn` or `fail` |
| `rules` | The distinct rules that fired |
| `findings` | `{rule, severity, pointer, message}`; `pointer` is a JSON pointer into the body, `header:<name>`, `status`, `content-type` or `operation` |

Exchanges carry `run_id`, `walk_id`, `seq`, `ts`, `interaction_id`, `method`, `path`, `query`,
`operation`, `consent` (the bearer token), `request_headers` (without `authorization`),
`request_body`, `status`, `response_headers` and `response_body`. The bodies are kept as text,
so each route can be parsed with its own schema.

## Settings

| Variable | Default | |
|---|---|---|
| `TARGET` | `http://proxy.ofb.svc` | What to walk |
| `BALANCE_CONSENTS` | `2` | New consents per customer that read every balance again |
| `SPECS_DIR` | `/app/specs` | The pinned specs (baked into the local image by `make validator-image`) |
| `RUN_ID` | the proxy's, else generated | Prefix when the target is not the proxy |
| `S3_ENDPOINT`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `RESULTS_BUCKET`, `AWS_REGION` | SeaweedFS, `lakehouse` | Result store (`validator-credentials` Secret) |

The specs are not committed (decision 10). `make validator-image` passes `specs/upstream` to
`docker build` as a named context, and they are baked into the local image only.

## Running it

```bash
make validator-test    # in process, over the proxy and the real transmitter
make validate          # one run as a Job in the cluster (infra/k8s/validator.yaml)
make validator-smoke   # joins that run's results with the labels, prints the outcome per class
```

`make validate` deletes the previous Job and starts a new one. It prints the summary
(`CONTRACT {...}`: exchanges, verdicts, findings per rule) and fails only if the run itself
fails, whatever it finds. After the run, the validator asks the proxy to write its buffered
labels (`POST /_proxy/flush`).
