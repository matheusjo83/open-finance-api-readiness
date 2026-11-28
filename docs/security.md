# Security layer (FAPI-BR)

The transmitter's authorisation server and how it is tested (backlog item 19, v2.0.0; decision
48). The target is the Open Finance Brasil Financial-grade API Security Profile v2.2.0 (portal
page 1675395195, version 2 of 2026-04-02): "an implementation of the FAPI-1-Advanced profile"
with extra requirements. It says nothing of FAPI 2.0 or of a date to move to it.

```bash
make security-test      # the PKI, the realm and the plan configuration (no cluster)
make auth-deploy        # Keycloak, the gateway, the transmitter's and the walk's credentials
make security-deploy    # the above and the conformance suite
make conformance        # one run of the suite's Brazil plan against Keycloak and the transmitter
```

Stages 1 to 3 of 4: Keycloak and the conformance suite in the cluster, the suite run headless
against a clean Keycloak and the transmitter as resource server, behind an mTLS gateway (2a),
the contract validator's walk with the real flows through the gateway and the defect proxy (2b,
[validator.md](validator.md#tokens-and-the-way-in-fapi-br-decision-50)), and the
[variants](#variants-stage-3): one defect per run and what the suite detects of them (3). The
detection report comes in stage 4.

## Components

All in the namespace `security`: Keycloak and the gateway in `infra/k8s/auth.yaml` (also in the
platform job, for the walk), the suite in `infra/k8s/security.yaml`.

| Component | What | Pinned |
|---|---|---|
| `keycloak` | The authorisation server: realm `ofb`, HTTPS only, the client certificate requested, H2 in the pod (the realm is imported at every start) | `quay.io/keycloak/keycloak:26.8.2` by digest (Apache-2.0) |
| `conformance` | The OpenID conformance suite's server in dev mode (no login), behind an nginx TLS front as in its own docker-compose.yml | `registry.gitlab.com/openid/conformance-suite:release-v5.3.5` and its nginx, by digest (MIT) |
| `conformance-mongo` | The suite's database | `mongo:6.0.13` by digest |
| `gateway` | The transmitter's mTLS gateway: TLS as v2.2.0 6.4, the client certificate verified against our CA and forwarded (`infra/security/gateway.conf`); only `/open-banking/`. Two doors: port 443 to the transmitter (the suite), port 8443 through the defect proxy (the walk) | the suite's nginx image |
| `transmitter` (namespace `ofb`) | The resource server ([below](#the-transmitter-as-resource-server)); its credentials for Keycloak in the Secret `transmitter-security` | `ofar/transmitter:local` |
| Job `conformance-run` | The suite's own `scripts/run-test-plan.py`, headless, then every module's outcome from the suite's API | `ofar/security-tools:local`; the three scripts by commit `246b2c1` and SHA-256 |

`services/security` (`security_tools`) makes what the pods read, into `.cache/security`, and
`make security-deploy` puts it in Secrets; no key is committed:

- **PKI** (`pki.py`): a CA of our own; TLS certificates for the three Services
  (`<name>.security.svc`); the transmitter's mTLS certificate and signing key for its calls to
  Keycloak; three clients (the suite's two and the walk's `walker`), each with an mTLS
  certificate whose subject carries the
  organisation id (OU) and the software statement id (UID), as the Brazilian certificate
  standard does, and a signing (PS256) and an encryption (RSA-OAEP) key. The ids are stable
  (`uuid5`); the keys are made once and kept until `.cache/security/pki` is deleted. Real
  certificates come from ICP-Brasil through the Directory; there is no Directory here.
- **Realm** (`realm.py`): adapted from the Open Finance Brasil realm of Keycloak's OAuth SIG
  (keycloak/kc-sig-fapi, Apache-2.0), which passes the suite's Brazil plan.
- **Provider jar** (`providers.py`): the script mapper `infra/security/authorise-consent.js` and
  its descriptor, the way Keycloak's `scripts` feature takes them (a jar is a zip; no Java).
- **Plan configuration** (`suite.py`): the two clients, their certificates, the resource URLs and
  the browser automation that logs the test users in.

## What the realm sets, against v2.2.0

| Requirement (v2.2.0) | In the realm |
|---|---|
| private_key_jwt (5.1 item 2) | `clientAuthenticatorType: client-jwt`, PS256 assertions |
| PAR required (5.1 item 3); `request_uri` at least 120 s (item 16), used once (item 22) | `require.pushed.authorization.requests`; `parRequestUriLifespan` 120 (Keycloak's default is 60) |
| PKCE (item 18) | client profile executor `pkce-enforcer`, S256 |
| `code id_token` (item 12) | the implicit flow on (Keycloak needs it for the hybrid response type) |
| Access tokens of 300 to 900 s (item 11) | 360 s (at 300, Keycloak's `expires_in` is 299 when a second turns between issuing and answering: the suite's `FAPIBrazilValidateExpiresIn` failed the baseline on CI once) |
| Refresh tokens without rotation (item 13) | executor `suppress-refresh-token-rotation` |
| acr `urn:brasil:openbanking:loa2` (item 6) | `acr.loa.map` of the realm and clients |
| id_token encrypted with RSA-OAEP and A256GCM (5.1.1, 6.3) | `id.token.encrypted.response.*`; the SIG uses A128GCM |
| JWS with PS256 (6.2) | every signature: client assertions, request objects, access and id tokens |
| TLS: the two ECDHE-RSA GCM suites (6.4) | `--https-cipher-suites` (with TLS 1.3) |
| Certificate-bound tokens (FAPI 1 Advanced) | `tls.client.certificate.bound.access.tokens`: `cnf.x5t#S256` |
| Dynamic scope `consent:<id>` (7) | a parameterized client scope (Keycloak feature `parameterized-scopes`, called `dynamic-scopes` before 26.7) |
| Product scopes in the discovery document (item 23) | one client scope each |

| No customer data in the id_token unless asked for | `profile` and `email` are optional client scopes, not default |

All of the realm's clients fall under one client policy: Keycloak's `fapi-1-advanced` profile
plus ours (PKCE, no rotation). `make security-test` checks each row above.

## The transmitter as resource server

Requests through the gateway carry the certificate nginx verified and the gateway's marker
(`services/transmitter/src/transmitter/security.py`). For those the transmitter is a FAPI-BR
resource server; every refusal is in the specs' error envelope:

| Check | Refused with |
|---|---|
| A bearer token, a JWT signed with PS256 by a key of Keycloak's JWKS, issued by the realm, with the transmitter (`ofar-transmitter`) in `aud`, not expired | 401 |
| Bound to the certificate of the request: `cnf.x5t#S256` (RFC 8705 3.1) | 401 |
| Still active at Keycloak (token introspection, RFC 7662, private_key_jwt over mTLS): a token Keycloak revoked, as after a reused authorisation code, is refused at once | 401 |
| Consents endpoints: scope `consents`; a client sees only the consents it created | 403 |
| Accounts: scope `accounts` and `consent:<id>` naming an AUTHORISED consent of the same client; revoking the consent ends its tokens | 403 (scope), 401 (consent) |
| Renewal (`POST .../extends`): `consent:<id>` of the consent in the path | 403 |

The origin of the limits is the organisation id of the certificate (its OU), not the
`x-mock-client-org-id` stand-in. Requests that do not come through the gateway keep the stand-ins
of [transmitter.md](transmitter.md): the tests in process, the proxy's own calls and the limits'
k6 load, which measures counting, not authentication (decision 50). The seeded consents belong
to the walk's client (`TRANSMITTER_SEED_CLIENT`).

**How a consent is approved.** In Open Finance the customer approves at the transmitter; here the
authorisation server is Keycloak, and approving inside it would need a Java extension of ours. A
script mapper on the client scope `consent` (`infra/security/authorise-consent.js`, feature
`scripts`) calls the transmitter's stand-in `POST /mock/consents/<id>/authorise` with the CPF of the
customer who logged in when Keycloak issues a customer's access token for `consent:<id>`: after the
login and the grant screen, so a customer who rejects leaves the consent awaiting. The transmitter
approves only a consent of that CPF that is still awaiting. Client credentials tokens are left
alone.

## The conformance run

Plan `fapi1-advanced-final-test-plan` with `client_auth_type=private_key_jwt`,
`fapi_profile=openbanking_brazil`, `fapi_auth_request_method=pushed`,
`fapi_response_mode=plain_response`: the row Keycloak's OAuth SIG runs for FAPI-BR v2. Dynamic
client registration is not tested (it needs the Directory; Keycloak was certified without it too).

Before each authorisation the suite gets a `client_credentials` token (scope `consents`), creates
a consent at `consentUrl`, adds `consent:<consentId>` to the scope, pushes the request (PAR) and
lets its browser automation (HtmlUnit, inside the suite) log in: `customer1` approves,
`customer2` rejects (module `user-rejects-authentication`). Keycloak approves the consent at the
transmitter as it issues the token (above). The suite then calls `resourceUrl` with the access
token over mTLS, through the gateway, and tries tokens the transmitter must refuse: client1's
token with client2's certificate, a token whose authorisation code was reused. The consents are
for a synthetic CPF that is not one of the transmitter's customers, so the accounts list is empty
(the suite only needs a 200).

The runner runs the modules one at a time (the configuration has an alias, so they share the
callback) and, by default, gives up after three in a row that do not complete; the Job lifts that
limit. `security_tools run` reads the plan's id from the runner's output, collects every module's
status, result and the conditions that failed or warned (with the requirements they cite) from the
suite's API, and judges each module (`MODULE` lines and `results.json`):

| Outcome | When |
|---|---|
| `passed`, `skipped` | As the suite says |
| `review` | The suite waits for a person to look at evidence (below), and nothing failed |
| `known` | Only the conditions listed for the module in `suite.KNOWN` failed or warned |
| `unexpected` | Anything else, including a module that did not complete |

The run fails on any `unexpected` module; the runner's own exit code is ignored (it fails on any
failure, expected or not).

### Evidence for the review placeholders

Where the authorisation server may show an error page instead of redirecting back (PAR 7.3: a
request_uri that is reused, expired or another client's; a request without a request object),
Keycloak shows one, and the suite waits for a screenshot of it, then marks the module REVIEW.
Its scripted browser (HtmlUnit) takes no screenshots, so a headless run waited 240 s per module and
the next module interrupted it. `evidence.py` watches the running modules: once the browser has
stopped on an error page of the authorisation server (4XX or 5XX; not back at the suite's
callback, where there is nothing to show), it uploads a PNG of the status and visible text of the last HTTP response the
browser got, headed "Not a screenshot". The plan's automation has already waited for the error
text on that page. The module stays REVIEW: the image is evidence for a reviewer, not a verdict.

A page the server answered with success is not one: the login page of a request it accepted (a
long nonce, a request_uri used again before the first login completed) is on the browser's way
to the callback. Filling the placeholder there ends the module, and the callback that follows
interrupts it: the long-nonce module failed main's baseline so once, when the uploader's sweep
came before the login.

### The clean baseline

2026-11-27, kind on a laptop, stage 2a: the 67 modules of the plan in 175 seconds (the expired
request_uri module alone waits its 120 s). Stage 1, with a stand-in resource server that checked
nothing, had 54 passed and 8 known: the three modules that call the resource server with a token
it must refuse pass now.

| Outcome | Modules |
|---|---|
| passed | 56 |
| known | 6 |
| review | 4 (with evidence: the four error pages above) |
| skipped | 1 (`ensure-mtls-holder-of-key-required-at-non-alias-token-endpoint`: Keycloak has no mTLS aliases) |
| unexpected | 0 |

The known outcomes (`suite.KNOWN`), each tied to the conditions that must be its only failures:

| Modules | Condition | Why |
|---|---|---|
| `par-ensure-client-assertion-with-{wrong-aud,wrong-iss,wrong-sub,invalid-signature}-fails` (4) | `CheckParEndpointHttpStatusIs400Allowing401ForInvalidClientError` | Keycloak 26.8.2 answers a client assertion it rejects at the PAR endpoint with 401 and `invalid_request`; RFC 6749 5.2 wants 400 for `invalid_request` (401 only for `invalid_client`) |
| `test-array-as-audience-for-client-JWT-assertion` | `CheckPAREndpointResponse201WithNoError` | Keycloak accepts only the issuer as a client assertion's audience (its fix of CVE-2025-27370, since 26.2); the suite still expects an array to be accepted. Keycloak's OAuth SIG turns the check off to pass; the realm keeps it on |
| `fapi1-advanced-final` (a warning) | `EnsureIdTokenDoesNotContainNonRequestedClaims` | Keycloak 26.8.2 puts `typ` in every id_token, and the suite warns on any claim it did not ask for. The claims of `profile` and `email`, which it warned on too, are sent only when asked for now |

Building it found, besides the findings below: the resource server's TLS is checked too (the
gateway has the v2.2.0 ciphers), the bearer scheme in any case (RFC 6750), and two of the SIG's
browser overrides that left the suite waiting for a visit; those modules use `match-limit` now
(the first visit logs in or only opens the page, the second ends on the error page).

### In CI

`make security-deploy conformance` runs the whole plan in a job of its own (`security`), in its
own kind cluster and in parallel with the platform job, since it needs none of the lakehouse
(about 2.6 minutes of suite time plus the images and Keycloak's start), gated as above; its log
is in the `security-reports` artifact (`logs/conformance.log`). `make security-test` runs in the
checks job.

## Variants (stage 3)

```bash
make security-deploy VARIANT=long-access-token   # Keycloak and the transmitter with one defect
make conformance                                   # one run, labelled with the deployed variant
make variants                                      # every variant, then the clean baseline again
```

Each variant breaks one requirement of v2.2.0, at the authorisation server (a patch of the clean
realm) or at the resource server (a check the transmitter skips, `TRANSMITTER_DEFECTS`), as
`services/security/src/security_tools/variants.py` lists them with the requirement they break.
The unit is the run, labelled with its variant; the suite **detects** a variant when at least
one module does other than the clean baseline (an `unexpected` outcome, against `suite.KNOWN`).
Which modules do is measured, not assumed: `make variants` runs the whole plan once per variant
and writes the runs' `CONFORMANCE` lines and the matrix below into `reports/variants/`. A
variant's run exits 0 whatever it found; the baseline stays gated.

Keycloak has no setting that makes a `request_uri` reusable (stage 1 listed it as a variant); a
`request_uri` of 60 seconds, the lifespan the suite checks against, takes its place.

### What the suite detects

2026-11-28, kind on a laptop: the clean baseline, the 11 variants and the baseline again, one plan
run each (about 4 minutes a run, 52 in all; `reports/variants/`).

| Variant | Layer | Requirement | Detected | Modules | Conditions that failed (not in the baseline) |
|---|---|---|---|---|---|
| baseline | | | clean | 0 | none |
| `par-optional` | authorisation-server | 5.1 item 3: PAR is required | **no** | 0 | none |
| `pkce-optional` | authorisation-server | 5.1 item 18: PKCE with S256 | yes | 2 | `EnsureInvalidRequestError` |
| `id-token-rs256` | authorisation-server | 6.2: PS256 for every JWS | yes | 27 | `FAPIBrazilValidateExpiresIn`, `FAPIBrazilValidateIdTokenSigningAlg`, `FAPIValidateIdTokenSigningAlg` |
| `long-access-token` | authorisation-server | 5.1 item 11: access tokens of 300 to 900 seconds | yes | 15 (+1 interrupted) | `FAPIBrazilValidateExpiresIn` |
| `refresh-rotation` | authorisation-server | 5.1 item 13: refresh tokens are not rotated | yes | 1 | `ValidateRefreshTokenNotRotated` |
| `id-token-unencrypted` | authorisation-server | 5.1.1 and 6.3: the id_token encrypted (RSA-OAEP, A256GCM) | yes | 27 | `ValidateIdTokenEncrypted` |
| `request-uri-60s` | authorisation-server | 5.1 item 16: a request_uri lives at least 120 seconds | **no** | 0 | none |
| `unbound-token` | resource-server | FAPI 1 Advanced 6.2.1-1 (via v2.2.0 5.1): tokens bound to the client's certificate | yes | 2 | `EnsureHttpStatusCodeIs4xx` |
| `expired-token` | resource-server | RFC 6750 3.1 (via v2.2.0 5.1 item 11): an expired token is refused | **no** | 0 | none |
| `foreign-consent` | resource-server | 7: the token's consent:<id> of the client's own consent | **no** | 0 | none |
| `no-introspection` | resource-server | FAPI 1 Advanced via RFC 6749 4.1.2: a revoked token is refused | yes | 1 | `EnsureHttpStatusCodeIs4xx` |
| baseline | | | clean | 0 | none |

The suite detects **7 of the 11**. Most of the rows with many modules are one condition repeated: every module that ends
with a token checks its `expires_in` and the id_token's algorithm and encryption. One
`id-token-rs256` module also failed `FAPIBrazilValidateExpiresIn`, once: the 300-second
tokens of that measurement, at the edge (now 360 s, above).

Its four gaps, each a requirement of v2.2.0 that nothing in the plan exercises:

- **`par-optional`**: in the PAR variant of the plan the suite always pushes; no module sends a
  request object by value to the authorisation endpoint and expects a refusal.
- **`request-uri-60s`**: `FAPIBrazilOBCheckPARResponseExpiresIn` checks at least 60 seconds
  ("the minimum expiration time of request_uri must be 60 seconds"); v2.2.0 says 120.
- **`expired-token`**: no module waits for a token to expire (360 seconds) before calling the
  resource server.
- **`foreign-consent`**: the suite's two clients each use their own consent; none tries the
  other's.

Detection here means a module whose outcome is not the baseline's. A module the suite only
interrupted (its own name as the failed condition: the run's timing) is **noise**, counted apart:
in a first pass of the same runs, before noise was told apart, one such module made `foreign-consent`
and `request-uri-60s` look detected. Stage 4 turns this into the detection report, with
detectors of our own for the gaps.

## Findings

- **The suite checks a `request_uri` of 60 s**: `FAPIBrazilOBCheckPARResponseExpiresIn` cites
  "the minimum expiration time of request_uri must be 60 seconds"; v2.2.0 (5.1 item 16) says
  120. Keycloak's default of 60 passes the suite and falls short of v2.2.0; the realm sets 120.
- **Keycloak 26.7 renamed dynamic scopes** to parameterized scopes (feature and attributes).
- **Keycloak's login on a second authorisation** shows the user name read-only and asks only for
  the password, so the automation's user name is optional.
- **Keycloak 26.8.2 answers a rejected client assertion at the PAR endpoint with 401
  `invalid_request`** (4 modules, above): a nonconformance of the authorisation server itself.
- **The suite's server will not start without login providers**, even in dev mode, which never
  uses them; the Deployment sets the placeholders of the suite's docker-compose.yml.
- **Keycloak 26.8.2 introspects a token only for a client in its audience** (`Client ... is not
  in the token audience`): the receivers' clients put the transmitter in `aud` (an audience
  mapper, access token only), and the transmitter checks it.
- **The id_token carried the customer's name and e-mail unasked** (the default client scopes
  `profile` and `email`); in stage 1 the warning hid under the module's failure. They are
  optional scopes now.
- **Keycloak's script mappers have no `LOG`** (authenticators have): the script takes a JBoss
  logger. A script that throws is logged and the token is still issued.
- **GitLab refuses curl on its raw URLs**: the runner's scripts come through GitLab's files API, by
  commit and SHA-256.
