# Specification findings

What `make lint-specs` reports on the pinned official specifications
(commit `9116cf0` of [OpenBanking-Brasil/openapi](https://github.com/OpenBanking-Brasil/openapi)).
Errors fail the lint; the warnings below are known and accepted.

## Consents 3.3.1

No findings.

The file starts with a UTF-8 byte order mark. Spectral accepts it; tools that read the specs
later (mock, contract tests) must decode with `utf-8-sig` or equivalent.

## Accounts 2.4.2

12 warnings, `oas3-operation-security-defined`: each of the six operations requires the scopes
`openid` (scheme `OpenId`) and `consent:consentId` (scheme `OAuth2Security`), but neither is
declared in `components.securitySchemes`. The `OAuth2Security` flow declares only `accounts`.

| Operation | Undeclared scopes |
|---|---|
| `GET /accounts` | `openid`, `consent:consentId` |
| `GET /accounts/{accountId}` | `openid`, `consent:consentId` |
| `GET /accounts/{accountId}/balances` | `openid`, `consent:consentId` |
| `GET /accounts/{accountId}/transactions` | `openid`, `consent:consentId` |
| `GET /accounts/{accountId}/transactions-current` | `openid`, `consent:consentId` |
| `GET /accounts/{accountId}/overdraft-limits` | `openid`, `consent:consentId` |

`consent:{consentId}` is a dynamic scope in Open Finance Brasil (one per consent), so it cannot be
listed as a fixed scope; `openid` is an OpenID Connect scope that `openIdConnect` schemes do not
enumerate. The warnings reflect a limit of OpenAPI 3.0 more than a defect in the API, and are
kept visible rather than silenced in the ruleset.
