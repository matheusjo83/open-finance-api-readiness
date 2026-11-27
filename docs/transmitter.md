# Synthetic transmitter

`services/transmitter` plays an Open Finance Brasil data transmitter (the institution that
holds the customer's accounts) for Consents 3.3.1 and Accounts 2.4.2. It is the clean baseline:
the defect proxy (item 2b) sits in front of it and breaks responses on purpose, and the
validators must tell the two apart. Decision 13 in [decisions.md](decisions.md) explains why it
replaces openapi-mock.

## Endpoints

| API | Endpoint | Status |
|---|---|---|
| Consents 3.3.1 | `POST /open-banking/consents/v3/consents` | implemented |
| | `GET /open-banking/consents/v3/consents/{consentId}` | implemented |
| | `DELETE /open-banking/consents/v3/consents/{consentId}` | implemented |
| | `POST .../consents/{consentId}/extends` | implemented (renewal) |
| | `GET .../consents/{consentId}/extensions` | implemented (renewal history) |
| Accounts 2.4.2 | `GET /open-banking/accounts/v2/accounts` | implemented (`accountType`, pagination) |
| | `GET .../accounts/{accountId}` | implemented |
| | `GET .../accounts/{accountId}/balances` | implemented |
| | `GET .../accounts/{accountId}/transactions` | implemented (12 months back, 12 ahead) |
| | `GET .../accounts/{accountId}/transactions-current` | implemented (7 days back, D-6) |
| | `GET .../accounts/{accountId}/overdraft-limits` | implemented (`{}` when the account has none) |

Every response carries the caller's `x-fapi-interaction-id`; a missing or invalid one is a 400
with a freshly generated id, as the specs require. Responses also carry `x-v` with the API
version. Errors use the `errors[]` / `meta` envelope with `application/json; charset=utf-8`.

## Mock-only stand-ins

These are outside the specifications and replace parts that belong to later layers:

| Stand-in | Real Open Finance | Why |
|---|---|---|
| Accounts endpoints and the renewal (`POST .../extends`) take `Authorization: Bearer <consentId>` | A FAPI-BR access token (`authorization_code`) bound to the consent | Security (FAPI-BR, mTLS, PAR) is a later layer. |
| Consents endpoints accept any bearer token | A `client_credentials` token from DCR | Same. |
| `POST /mock/consents/{consentId}/authorise` | The customer authenticates and approves at the transmitter | No authorisation server yet. |
| `GET /mock/seed` | — | Lists the seeded customers, consents and accounts so validators know what to call. |
| `GET /healthz` | — | Readiness probe. |
| `x-mock-client-org-id` request header | The `organizationId` of the caller's certificate | The origin and receiver of the [limits](#limits); without it, the fixed receiver id the telemetry uses. |

A consent covers every account of its customer (no account selection at authorisation).

## Consent lifecycle

| From | Event | To | `rejection` |
|---|---|---|---|
| — | `POST /consents` | `AWAITING_AUTHORISATION` | |
| `AWAITING_AUTHORISATION` | mock authorise | `AUTHORISED` | |
| `AWAITING_AUTHORISATION` | 60 minutes without approval | `REJECTED` | `ASPSP`, `CONSENT_EXPIRED` |
| `AWAITING_AUTHORISATION` | `DELETE` | `REJECTED` | `USER`, `CUSTOMER_MANUALLY_REJECTED` |
| `AUTHORISED` | `DELETE` | `REJECTED` | `USER`, `CUSTOMER_MANUALLY_REVOKED` |
| `AUTHORISED` | `expirationDateTime` passes | `REJECTED` | `ASPSP`, `CONSENT_MAX_DATE_REACHED` |
| `REJECTED` | `DELETE` | 422 `CONSENTIMENTO_EM_STATUS_REJEITADO` | |
| `AUTHORISED` | `POST .../extends` | `AUTHORISED`, with the new `expirationDateTime` (or none: indeterminate) | |
| `AWAITING_AUTHORISATION`, `REJECTED` | `POST .../extends` | 422 `ESTADO_CONSENTIMENTO_INVALIDO` | |

The time-based transitions are applied when the consent is read. `expirationDateTime` must be
in the future and at most 12 months ahead (422 `DATA_EXPIRACAO_INVALIDA`); it may be omitted.
Permissions must be known and not repeated; the permission-group rules (for example
`COMBINACAO_PERMISSOES_INCORRETA`) are not checked yet.

A renewal (`POST .../extends`) needs the consent itself as the bearer (the stand-in for the
customer's `authorization_code` token; otherwise 401), the `x-fapi-customer-ip-address` and
`x-customer-user-agent` headers (otherwise 400), and the customer who created the consent as
`loggedUser` (otherwise 403). The new `expirationDateTime` must be later than the current one
and at most 12 months ahead (422 `DATA_EXPIRACAO_INVALIDA`); without it, the consent becomes
indeterminate. Each renewal is recorded: `GET .../extensions` lists them newest first, each
with its `expirationDateTime`, the `previousExpirationDateTime` it replaced, `requestDateTime`,
the logged user and the two headers. Multiple approvers (`DEPENDE_MULTIPLA_ALCADA`) do not
apply: there are no business customers yet.

The accounts endpoints answer only for an `AUTHORISED` consent (otherwise 401), with the
permission the endpoint needs (otherwise 403) and for accounts of the consent's customer
(otherwise 404).

## Limits

The transmitter applies both limits of the Manual de APIs (IN 615, 5.1.1 and 5.2) at the
minimums of [operational.md](operational.md), in `transmitter/limits.py`:

| Limit | Counted per | Past it | Not counted |
|---|---|---|---|
| Traffic (TPM) | endpoint, origin, calendar minute (UTC) | 429 `LIMITE_TRAFEGO_EXCEDIDO` | the 429s themselves |
| Monthly | month (Brasília), endpoint, object (the consent for `GET /accounts`, the account below it), customer, receiver | 423 `LIMITE_OPERACIONAL_EXCEDIDO` | non-2XX; pages after the first (never refused either) |

The traffic limit is checked first; Consents have neither. Only an `AUTHORISED` consent is
counted for the month (the others get 401 anyway). `TRANSMITTER_LIMITS` raises limits, as JSON
such as `{"GET /accounts": {"tpm": 1200, "monthly": 10}}`; a value below the minimum, or a
limit on Consents, stops the transmitter at start. Two requests of the same key that pass the
check together may both be served: a limit may let more through, never fewer. The counters
live in memory, like the consents. The alta TPM by active consents (`alta_tpm`) is there and
tested, but every run stays in its first band (2500).

## Dataset

`TRANSMITTER_SEED` (default 42) and `TRANSMITTER_CUSTOMERS` (default 5) fix the dataset; the
dates are relative to the day the transmitter starts. The same seed and day always give the
same customers, accounts and transactions. Consents created at run time live in memory, so the
Deployment has one replica and a restart forgets them.

Each customer has one to three accounts (always one `CONTA_DEPOSITO_A_VISTA`). Seeded consents
(listed by `/mock/seed`):

| Consent | Customer | State |
|---|---|---|
| `urn:transmitter:seed-<n>-authorised` | each customer *n* | `AUTHORISED`, all accounts permissions, about a year left |
| `urn:transmitter:seed-<n>-expired` | each customer *n* | `AUTHORISED` until 35 days ago, so read as `REJECTED` (`CONSENT_MAX_DATE_REACHED`) |
| `urn:transmitter:seed-<n>-revoked` | each customer *n* | `REJECTED` (`CUSTOMER_MANUALLY_REVOKED`) |
| `urn:transmitter:seed-<n>-no-balances` | each customer *n* | `AUTHORISED` with `ACCOUNTS_READ` and `RESOURCES_READ` only |
| `urn:transmitter:seed-<n>-awaiting` | each customer *n* | `AWAITING_AUTHORISATION` (rejected 60 minutes after start) |

`/mock/seed` lists each customer's consents in this order, the authorised one first.

### Invariants

The business-rule layer (item 4) can rely on these, and the defect proxy breaks them on purpose.
`services/transmitter/tests/test_baseline.py` checks them through the API:

- Every account was opened within the last 12 months, so its whole history fits the
  `/transactions` window, starting with the opening deposit.
- `balances.availableAmount` equals the signed sum (credits minus debits) of the account's
  `TRANSACAO_EFETIVADA` transactions. The running balance never goes below zero.
- `LANCAMENTO_FUTURO` transactions are dated in the future and do not count in the balance.
- `transactionId` is unique and stable; `/transactions-current` returns a subset of `/transactions`.
- `GET /accounts` lists exactly the customer's accounts, and each one answers on `/accounts/{accountId}`.
- `blockedAmount` and `automaticallyInvestedAmount` are zero; overdraft limits exist only for
  `CONTA_DEPOSITO_A_VISTA` and are unused.
- CPFs and CNPJs carry valid check digits; amounts are in `BRL` with two decimals.
- A renewed consent reads with the expiration its last renewal set. Its renewal history is newest
  first, each renewal's `previousExpirationDateTime` is the `expirationDateTime` of the one
  before it, and the newest one's `expirationDateTime` is the consent's.

## Tests

`make transmitter-test` runs the transmitter in process and validates every request and response
against the pinned specs with openapi-core (status, headers, content type, body). It covers the
seeded resources (all pages), the consent lifecycle, the error paths (400, 401, 403, 404, 422)
and the invariants above. One test lowers the currency to `brl` and expects the validation to
fail, so the harness is known to catch an out-of-spec response.

`tests/test_limits.py` checks the limits: the table against the requirements, the minute, the
month and its turn at midnight in Brasília, what is not counted, and the overrides.

openapi-core does not check the `openIdConnect` security scheme; security is a later layer.
None of the pinned schemas sets `additionalProperties: false`, so an undeclared extra field
passes schema validation: the defect catalogue has to account for that.
