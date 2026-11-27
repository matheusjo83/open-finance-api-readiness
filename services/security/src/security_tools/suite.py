"""The OpenID conformance suite's configuration for the Open Finance Brasil plan against our
Keycloak, with the browser automation that logs the test users in.

Plan and variant as Keycloak's OAuth SIG runs it for FAPI-BR v2 (kc-sig-fapi,
conformance-tests-env/conformance-suite/fapi-conformance-suite-configs/of-br-fapi1-advanced,
Apache-2.0): fapi1-advanced-final-test-plan, private_key_jwt, openbanking_brazil, PAR, plain
response mode. The per-module browser overrides are theirs, with Keycloak's URLs replaced by ours.
"""

import json
from pathlib import Path

from . import realm

PLAN = "fapi1-advanced-final-test-plan"
VARIANT = {"client_auth_type": "private_key_jwt", "fapi_profile": "openbanking_brazil",
           "fapi_auth_request_method": "pushed", "fapi_response_mode": "plain_response"}


STUB = ("stage 1: the stand-in resource server does not check the token, so it cannot refuse one "
        "that is revoked or not bound to the client's certificate (the transmitter does, from stage 2)")
PAR_401 = ("Keycloak 26.8.2 answers a client assertion it rejects at the PAR endpoint with 401 and "
           "error invalid_request; RFC 6749 5.2 wants 400 for invalid_request (401 only for invalid_client)")
AUD_ARRAY = ("Keycloak 26.8.2 accepts only the issuer as the audience of a client assertion (its fix of "
             "CVE-2025-27370, since 26.2); the suite still expects an array to be accepted. Keycloak's "
             "OAuth SIG turns the check off to pass; the realm keeps it on")
# Outcomes of the clean baseline that are expected, with the conditions that must be the only
# failures (or warnings) of the module and why. Anything else that does not pass is unexpected.
KNOWN = {
    "fapi1-advanced-final": ({"EnsureHttpStatusCodeIs4xx"}, STUB),
    "fapi1-advanced-final-refresh-token": ({"EnsureHttpStatusCodeIs4xx"}, STUB),
    "fapi1-advanced-final-attempt-reuse-authorisation-code-after-one-second": ({"EnsureHttpStatusCodeIs4xx"}, STUB),
    **{f"fapi1-advanced-final-par-ensure-client-assertion-with-{case}-fails":
       ({"CheckParEndpointHttpStatusIs400Allowing401ForInvalidClientError"}, PAR_401)
       for case in ("wrong-aud", "wrong-iss", "wrong-sub", "invalid-signature")},
    "fapi1-advanced-final-test-array-as-audience-for-client-JWT-assertion":
        ({"CheckPAREndpointResponse201WithNoError"}, AUD_ARRAY),
}


def plan_name(modules: list[str] | None = None) -> str:
    name = PLAN + "".join(f"[{k}={v}]" for k, v in VARIANT.items())
    return name + (":" + ",".join(modules) if modules else "")


def _login(user: str, password: str, *, accept: bool, auth: str, suite_base: str) -> list[dict]:
    actions = f"{auth.rsplit('/protocol/', 1)[0]}/login-actions"
    return [{
        "match": f"{auth}*",
        "tasks": [
            {"task": "Initial login", "match": f"{actions}/authenticate*", "optional": accept,
             # On a second authentication in the same session Keycloak shows the user name read-only
             # and asks only for the password, so the user name is optional.
             "commands": [["text", "id", "username", user, "optional"], ["text", "id", "password", password],
                          ["click", "name", "login"]]},
            {"task": "Authorize client", "match": f"{actions}/required-action*", "optional": accept,
             "commands": [["click", "name", "accept" if accept else "cancel"]]},
            {"task": "Verify complete", "match": f"{suite_base}/test/a/{realm.ALIAS}/callback*",
             "commands": [["wait", "id", "submission_complete", 10]]},
        ],
    }]


def _error_page(auth: str, xpath: str, task: str = "Verify complete") -> list[dict]:
    return [{"match": f"{auth}*", "tasks": [{"task": task, "match": f"{auth}*",
                                              "commands": [["wait", "xpath", xpath, 10]]}]}]


def _first(visit: list[dict]) -> list[dict]:
    """Only the first visit of the authorisation endpoint (the suite's match-limit)."""
    return [{**entry, "match-limit": 1} for entry in visit]


def _open_only(auth: str) -> list[dict]:
    """Loads the login page and stops there, without logging in."""
    return [{"match": f"{auth}*", "tasks": [{"task": "Open the login page only", "match": "*",
                                              "commands": [["wait", "id", "username", 10]]}]}]


def build(manifest: dict, pki_dir: Path, *, issuer: str, suite_base: str, resource_base: str,
          cpf: str) -> dict:
    auth = f"{issuer}/protocol/openid-connect/auth"
    users = list(realm.USERS.items())
    invalid_request = _error_page(auth, "//p[text()='Invalid Request']")
    login = _login(*users[0], accept=True, auth=auth, suite_base=suite_base)

    def client(name: str) -> dict:
        entry = manifest["clients"][name]
        return {"client_id": entry["client_id"], "scope": "openid accounts",
                "jwks": json.loads((pki_dir / entry["jwks"]).read_text())}

    def mtls(name: str) -> dict:
        entry = manifest["clients"][name]
        return {"cert": (pki_dir / entry["cert"]).read_text(), "key": (pki_dir / entry["key"]).read_text(),
                "ca": (pki_dir / manifest["ca"]).read_text()}

    return {
        "alias": realm.ALIAS,
        "description": "FAPI-BR v2.2.0: the transmitter's Keycloak (PAR, private_key_jwt, PS256)",
        "server": {"discoveryUrl": f"{issuer}/.well-known/openid-configuration"},
        "client": client("client1"),
        "client2": client("client2"),
        "mtls": mtls("client1"),
        "mtls2": mtls("client2"),
        "resource": {
            "resourceUrl": f"{resource_base}/open-banking/accounts/v2/accounts",
            "consentUrl": f"{resource_base}/open-banking/consents/v3/consents",
            "brazilCpf": cpf,
        },
        "consent": {"productType": "personal"},
        "browser": login,
        "override": {
            "fapi1-advanced-final-user-rejects-authentication":
                {"browser": _login(*users[1], accept=False, auth=auth, suite_base=suite_base)},
            "fapi1-advanced-final-ensure-registered-redirect-uri":
                {"browser": _error_page(auth, "//p[text()='Invalid parameter: redirect_uri']")},
            "fapi1-advanced-final-ensure-matching-key-in-authorization-request":
                {"browser": _error_page(auth, "//*", "Expect an immediate redirect back with an error")},
            "fapi1-advanced-final-ensure-authorization-request-without-request-object-fails":
                {"browser": invalid_request},
            "fapi1-advanced-final-ensure-redirect-uri-in-authorization-request":
                {"browser": _error_page(auth, "//*", "Expect the redirect uri mismatch page")},
            "fapi1-advanced-final-ensure-signed-request-object-with-RS256-fails": {"browser": invalid_request},
            "fapi1-advanced-final-ensure-request-object-with-invalid-signature-fails": {"browser": invalid_request},
            "fapi1-advanced-final-ensure-request-object-signature-algorithm-is-not-none": {"browser": invalid_request},
            # The first use of a request_uri logs in; its reuse must end on an error page.
            "fapi1-advanced-final-par-attempt-reuse-request_uri":
                {"browser": _first(login) + invalid_request},
            # The page is only opened with the request_uri, then the same request_uri logs in.
            "fapi1-advanced-final-par-ensure-reused-request-uri-prior-to-auth-completion-succeeds":
                {"browser": _first(_open_only(auth)) + login},
            "fapi1-advanced-final-par-attempt-to-use-expired-request_uri": {"browser": invalid_request},
            "fapi1-advanced-final-par-attempt-to-use-request_uri-for-different-client": {"browser": invalid_request},
        },
    }
