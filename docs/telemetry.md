# Telemetry

Both sides of every Open Finance exchange report it as they would to the PCM (Plataforma de
Coleta de Métricas): the contract validator as the client (the receiving institution) and the
defect proxy as the server (the transmitting institution's gateway). The reports are what the
operational metrics are computed from (backlog items 15 and 16): response time, availability,
overload and reporting ([operational.md](operational.md)).

## Where

| Side | Written by | Object |
|---|---|---|
| CLIENT | contract validator, with its results | `s3://lakehouse/pcm/client/run=<run_id>/part-NNNNN-<walk_id>.jsonl` |
| SERVER | defect proxy, with its labels | `s3://lakehouse/pcm/server/run=<run_id>/part-NNNNN-<pod>.jsonl` |

Each line is `{"run_id", "sent_at", "report"}` (the client adds `walk_id`). `report` is the body
the PCM would receive, valid against its OpenAPI file but for the cases below; `sent_at` is when
it was handed to the store, for the reporting deadlines (08:00 of the next day, 7 days).

The PCM pairs the two reports of an exchange by `fapiInteractionId` and then compares
`clientOrgId`, `serverOrgId`, `statusCode`, `httpMethod` and `endpoint`: the same five fields
make a report `PAIRED` or `PAIRED_INCONSISTENT`. `make telemetry-smoke` pairs a run's reports in
the cluster. In CI every client report is paired. The server also has reports of calls nobody
reported as a client (`make proxy-smoke`): the PCM would mark them `UNPAIRED`.

## What is measured

- **Client**: from just before the request to the last byte of the response, before any
  processing (Manual de APIs 5.3.2 V). The walk times the call; the HTTP client reads the whole body.
- **Server**: from the request's arrival to the last byte sent (5.3), measured by an ASGI
  middleware around the whole proxy. Its time includes what the proxy does to inject a defect
  (a second call to the transmitter for `consent-not-enforced`, the history for
  `extension-not-reflected`). That is the server's own doing, as a slow gateway would be.

`processTimespan` is in whole milliseconds, rounded up: the portal leaves out reports with 0, and
calls inside the cluster take a few milliseconds. The status is the one the client got, defect
included. A request with an unhandled error is reported as the 500 it answers.

## Fields

Both: `role`, `endpoint` (the template, as the PCM accepts it), `httpMethod`, `statusCode`,
`timestamp` (UTC, milliseconds), `processTimespan`, `clientOrgId`, `serverOrgId`,
`fapiInteractionId` (when it is a UUID: the client's, or the one the server generated) and
`additionalInfo`. The client adds `clientSSId` and `endpointUriPrefix`.

The institutions' ids are fixed UUIDs (`uuid5` of local names), equal on both sides; a real
server takes the client's from its certificate (the FAPI layer, later).

`additionalInfo` follows the portal's pages for Consents and Accounts:

| Field | Side | Operations | When |
|---|---|---|---|
| `tokenId` | client | all | 2xx and 3xx: the first 72 bits of SHA-256(token + pepper), Base64URL |
| `consentId` | client | Accounts (the bearer), Consents | always; on a refused POST, not |
| `authorisationFlowIntent`, `personType` | client | `POST /consents` | always: `HYBRID_FLOW`; `PJ` when the request has `businessEntity` |
| `journeyIsLinked` | client | `POST /consents`, `GET /consents/{consentId}` | always: `FALSE` unless `journey.isLinked` |
| `expirationDateTime` | client | Consents, POST and GET | 2xx, from the response |
| `status`, `rejectedBy`, `rejectionReasonCode` | client | `GET /consents/{consentId}` | 2xx; the last two for a `REJECTED` consent |
| `dropReason` | server | `POST /consents` | always: `NONE` for a 201 (nothing checks credentials here), `NO_CREDENTIAL` for 4xx, `CREDENTIAL_UNAVAILABLE` for 5xx |

No token, account or customer identifier leaves in a report: the consent id is the PCM's own
field, and the token goes only as its hash.

## The PCM's OpenAPI file and the portal

`make specs` downloads the PCM's OpenAPI file, the one the portal links as Telemetria v3.9.3, at
a fixed commit, checked by SHA-256. Its `info.version` is always 1.0.0. The file and the portal's
pages disagree on some domains; the reports follow the pages, which are newer
(`pcm-spec-domains` in the requirements):

| Field | File | Portal |
|---|---|---|
| `rejectedBy` | `INICIADORA`, `USUARIO`, `DETENTORA` | `USER`, `ASPSP`, `TPP` |
| `dropReason` (server) | `'NO_CREDENTIAL, NONE'` as one value, no `CREDENTIAL_UNAVAILABLE` | five values |
| `authorisationFlowIntent` | `CIBA_FLOW` | `CIBA` |
| `companyProfileInfo` | `naturezaJurídica` | `naturezaJuridica` |

The tests validate every report of a walk against the file and accept only the first two
disagreements, the ones the walk meets; anything else fails.

## Limits

- A call the client gives up on after 15 seconds should be reported with 408. The walk stops on
  a timeout instead; no defect delays responses yet.
- The proxy's upstream timeout is 10 seconds and gives a 500, not the 504 of the manual.
- The reports are written, not sent: the PCM's statuses (`PAIRED`, ...) are computed by the
  operational job from both sides (item 16).
