"""The Keycloak realm of the transmitter's authorisation server, configured for FAPI-BR v2.2.0
(Open Finance Brasil Financial-grade API Security Profile, portal page 1675395195 version 2,
2026-04-02): FAPI 1 Advanced with private_key_jwt, PAR required, PKCE, PS256, response type
`code id_token`, the id_token encrypted (RSA-OAEP, A256GCM), access tokens of 300 s bound to
the client's certificate, refresh tokens without rotation, acr urn:brasil:openbanking:loa2 and
the parameterized scope consent:<id>.

Adapted from the Open Finance Brasil realm of Keycloak's OAuth SIG (keycloak/kc-sig-fapi,
conformance-tests-env/test-target/keycloak/keycloak/realms/realm-of-br-fapi1-advanced.json,
Apache-2.0), which passes the OpenID conformance suite's Brazil plan. Changes: our own keys and
clients; PAR required and PS256 access tokens; A256GCM as v2.2.0 section 6.3 says (the SIG uses
A128GCM); a parameterized consent scope instead of the fixed `consent:1`; the defaults of a new
26.8.2 realm for the built-in scopes.

The consent scope carries a script mapper (providers.py, infra/security/authorise-consent.js)
that approves the consent at the transmitter when a customer's token is issued for it. The
transmitter, as resource server, has a client of its own for token introspection.
"""

import copy
import json
from pathlib import Path

from . import providers

REALM = "ofb"
ALIAS = "ofar"  # the suite's alias: its callback is <suite>/test/a/<alias>/callback
LOA2 = "urn:brasil:openbanking:loa2"
LOA3 = "urn:brasil:openbanking:loa3"
RESOURCE_SERVER_CLIENT = "ofar-transmitter"
ACCESS_TOKEN_SECONDS = 300
PAR_REQUEST_URI_SECONDS = 120
# Test users of the browser automation: the first approves, the second rejects (suite module
# fapi1-advanced-final-user-rejects-authentication). Synthetic, like everything else.
USERS = {"customer1": "customer1-password", "customer2": "customer2-password"}
# Scopes the API scopes the well-known document must list (v2.2.0 5.1 item 23) beyond ours.
PRODUCT_SCOPES = ("accounts", "consents", "resources", "customers", "credit-cards-accounts",
                  "invoice-financings", "financings", "loans", "unarranged-accounts-overdraft",
                  "bank-fixed-incomes", "credit-fixed-incomes", "variable-incomes",
                  "treasure-titles", "funds", "exchanges")
# services/security/src/security_tools/realm.py -> the repository root.
DEFAULT_SCOPES = Path(__file__).resolve().parents[4] / "infra/security/keycloak-default-scopes.json"


def _scope(name: str, **attributes) -> dict:
    return {"name": name, "protocol": "openid-connect",
            "attributes": {"include.in.token.scope": "true", "display.on.consent.screen": "true",
                           **attributes}}


def redirect_uris(suite_base: str) -> list[str]:
    base = f"{suite_base.rstrip('/')}/test/a/{ALIAS}/callback"
    return [base, base + "?dummy1=lorem&dummy2=ipsum"]


def client(entry: dict, suite_base: str) -> dict:
    return {
        "clientId": entry["client_id"],
        "name": f"Receiver {entry['organisation_id']}",
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "bearerOnly": False,
        # The consent screen lets the second test user reject (user-rejects-authentication).
        "consentRequired": True,
        "standardFlowEnabled": True,
        # response_type `code id_token` is a hybrid flow: Keycloak needs the implicit flow on.
        "implicitFlowEnabled": True,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": True,
        "fullScopeAllowed": True,
        "clientAuthenticatorType": "client-jwt",
        "redirectUris": redirect_uris(suite_base),
        "attributes": {
            "use.jwks.string": "true",
            "jwks.string": json.dumps(entry["public_jwks"]),
            "token.endpoint.auth.signing.alg": "PS256",
            "request.object.signature.alg": "PS256",
            "request.object.required": "request or request_uri",
            "require.pushed.authorization.requests": "true",
            "pkce.code.challenge.method": "S256",
            "tls.client.certificate.bound.access.tokens": "true",
            "access.token.signed.response.alg": "PS256",
            "id.token.signed.response.alg": "PS256",
            "id.token.encrypted.response.alg": "RSA-OAEP",
            "id.token.encrypted.response.enc": "A256GCM",
            "exclude.session.state.from.auth.response": "false",
            "use.refresh.tokens": "true",
            "client_credentials.use_refresh_token": "false",
            "acr.loa.map": json.dumps({LOA2: "1"}),
            "default.acr.values": LOA2,
        },
        # The transmitter in the access token's audience: Keycloak introspects a token only for a
        # client in it, and the transmitter checks it. Not in the id_token (the suite checks it
        # for claims it did not ask for).
        "protocolMappers": [{
            "name": "transmitter audience", "protocol": "openid-connect", "protocolMapper": "oidc-audience-mapper",
            "config": {"included.client.audience": RESOURCE_SERVER_CLIENT, "access.token.claim": "true",
                       "id.token.claim": "false", "introspection.token.claim": "true"},
        }],
        # profile and email only when asked for: the id_token carries no customer data the
        # receiver did not request (the suite warns on any such claim).
        "defaultClientScopes": ["basic", "acr", "roles", "web-origins"],
        "optionalClientScopes": ["offline_access", "profile", "email", "consent", *PRODUCT_SCOPES],
    }


def resource_server(entry: dict) -> dict:
    """The transmitter's client: it only introspects tokens (private_key_jwt over mTLS, as the
    realm's client policy wants of every client); no flow of its own."""
    return {
        "clientId": entry["client_id"],
        "name": "Transmitter (resource server)",
        "enabled": True,
        "protocol": "openid-connect",
        "publicClient": False,
        "standardFlowEnabled": False,
        "implicitFlowEnabled": False,
        "directAccessGrantsEnabled": False,
        "serviceAccountsEnabled": False,
        "fullScopeAllowed": False,
        "clientAuthenticatorType": "client-jwt",
        "attributes": {
            "use.jwks.string": "true",
            "jwks.string": json.dumps(entry["public_jwks"]),
            "token.endpoint.auth.signing.alg": "PS256",
            "tls.client.certificate.bound.access.tokens": "true",
        },
    }


def consent_scope() -> dict:
    # consent:<Consent Resource Id> (v2.2.0 section 7): a parameterized scope (Keycloak feature
    # parameterized-scopes, called dynamic-scopes before 26.7) of type string, any value.
    scope = _scope("consent", **{"is.parameterized.scope": "true", "parameterized.scope.type": "string"})
    scope["protocolMappers"] = [{
        "name": "authorise consent at the transmitter",
        "protocol": "openid-connect",
        "protocolMapper": providers.MAPPER_ID,
        "config": {"access.token.claim": "true", "id.token.claim": "false", "userinfo.token.claim": "false",
                   "introspection.token.claim": "false", "claim.name": "", "jsonType.label": "String"},
    }]
    return scope


def build(manifest: dict, suite_base: str, cpf: str, default_scopes: Path = DEFAULT_SCOPES) -> dict:
    """`cpf`: the customers' CPF (attribute `cpf`), which the consents the suite creates name."""
    defaults = json.loads(default_scopes.read_text())
    scopes = copy.deepcopy(defaults["clientScopes"])
    scopes += [_scope(name) for name in PRODUCT_SCOPES]
    scopes.append(consent_scope())
    return {
        "realm": REALM,
        "enabled": True,
        "sslRequired": "all",
        "registrationAllowed": False,
        "accessTokenLifespan": ACCESS_TOKEN_SECONDS,
        "defaultSignatureAlgorithm": "PS256",
        # No refresh token rotation (v2.2.0 5.1 item 13); also enforced by the client profile.
        "revokeRefreshToken": False,
        "attributes": {
            # Published as acr_values_supported in the discovery document.
            "acr.loa.map": json.dumps({LOA2: "1", LOA3: "2"}),
            # request_uri lives at least 120 s (v2.2.0 5.1 item 16; Keycloak's default is 60).
            "parRequestUriLifespan": str(PAR_REQUEST_URI_SECONDS),
        },
        "clientScopes": scopes,
        "defaultDefaultClientScopes": defaults["defaultDefaultClientScopes"],
        "defaultOptionalClientScopes": defaults["defaultOptionalClientScopes"],
        "clients": [*(client(entry, suite_base) for entry in manifest["clients"].values()),
                    resource_server(manifest["resource_server"])],
        "users": [{"username": u, "enabled": True, "email": f"{u}@example.com", "emailVerified": True,
                   "firstName": u, "lastName": "OFAR", "attributes": {"cpf": [cpf]},
                   "credentials": [{"type": "password", "value": p, "temporary": False}]}
                  for u, p in USERS.items()],
        "components": {"org.keycloak.keys.KeyProvider": [
            {"name": "ps256", "providerId": "rsa-generated",
             "config": {"keySize": ["2048"], "active": ["true"], "enabled": ["true"],
                        "priority": ["100"], "algorithm": ["PS256"]}},
            {"name": "rsa-oaep", "providerId": "rsa-enc-generated",
             "config": {"keySize": ["2048"], "active": ["true"], "enabled": ["true"],
                        "priority": ["100"], "algorithm": ["RSA-OAEP"]}},
            {"name": "hmac", "providerId": "hmac-generated",
             "config": {"priority": ["100"], "algorithm": ["HS512"]}},
            {"name": "aes", "providerId": "aes-generated", "config": {"priority": ["100"]}},
        ]},
        "clientProfiles": {"profiles": [{
            "name": "of-br-fapi-br",
            "description": "FAPI-BR v2.2.0 additions to Keycloak's fapi-1-advanced: PKCE, no refresh token rotation",
            "executors": [
                {"executor": "pkce-enforcer", "configuration": {"auto-configure": "true"}},
                {"executor": "suppress-refresh-token-rotation", "configuration": {}},
            ],
        }]},
        "clientPolicies": {"policies": [{
            "name": "of-br-fapi-br",
            "description": "Every client of the realm follows FAPI-BR v2.2.0",
            "enabled": True,
            "conditions": [{"condition": "any-client", "configuration": {}}],
            "profiles": ["fapi-1-advanced", "of-br-fapi-br"],
        }]},
    }
