"""The transmitter as resource server, through the gateway (security.py): tokens of a stand-in
issuer, signed and introspected in process, with certificates of a CA of the test. Every
response still validates against the pinned specifications (the `api` fixture's client)."""

from __future__ import annotations

import base64
import datetime as dt
import urllib.parse
import uuid

import jwt
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient

from transmitter.app import ACCOUNTS, CONSENTS, create_app
from transmitter.security import CERT_HEADER, GATEWAY_HEADER, Verifier, thumbprint

from .conftest import START, CheckedClient
from .test_baseline import consent_request

ISSUER = "https://keycloak.security.svc/realms/ofb"
AUDIENCE = "ofar-transmitter"


def _key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _b64(n: int) -> str:
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()


class Issuer:
    """A stand-in for Keycloak: its signing key, its JWKS and its introspection."""

    def __init__(self, clock) -> None:
        self.key, self.kid, self.clock = _key(), "ps256-test", clock
        self.revoked: set[str] = set()
        self.ca_key = _key()
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test CA")])
        self.ca_name = name

    def jwks(self) -> dict:
        pub = self.key.public_key().public_numbers()
        return {"keys": [{"kty": "RSA", "kid": self.kid, "use": "sig", "alg": "PS256",
                          "n": _b64(pub.n), "e": _b64(pub.e)}]}

    def introspect(self, token: str) -> dict:
        return {"active": token not in self.revoked}

    def certificate(self, organisation: str) -> x509.Certificate:
        now = dt.datetime.now(dt.UTC)
        subject = x509.Name([x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, organisation),
                             x509.NameAttribute(NameOID.COMMON_NAME, f"client of {organisation}")])
        return (x509.CertificateBuilder().subject_name(subject).issuer_name(self.ca_name)
                .public_key(_key().public_key()).serial_number(x509.random_serial_number())
                .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=1))
                .sign(self.ca_key, hashes.SHA256()))

    def token(self, client_id: str, cert: x509.Certificate, scope: str, *, seconds: int = 300,
              key=None, alg: str = "PS256", issuer: str = ISSUER, bound: bool = True,
              audience: list[str] | None = None) -> str:
        now = int(self.clock().timestamp())
        claims = {"iss": issuer, "aud": audience if audience is not None else [AUDIENCE, "account"], "azp": client_id, "sub": str(uuid.uuid4()), "scope": scope,
                  "iat": now, "exp": now + seconds, "jti": str(uuid.uuid4())}
        if bound:
            claims["cnf"] = {"x5t#S256": thumbprint(cert)}
        return jwt.encode(claims, key or self.key, algorithm=alg, headers={"kid": self.kid})


def gateway(cert: x509.Certificate | None) -> dict:
    headers = {GATEWAY_HEADER: "1"}
    if cert is not None:
        headers[CERT_HEADER] = urllib.parse.quote(cert.public_bytes(serialization.Encoding.PEM).decode())
    return headers


@pytest.fixture
def issuer(clock) -> Issuer:
    return Issuer(clock)


@pytest.fixture
def app(clock, issuer):
    verifier = Verifier(issuer=ISSUER, audience=AUDIENCE, jwks=issuer.jwks, introspect=issuer.introspect, clock=clock)
    return create_app(seed=42, customers=5, clock=clock, verifier=verifier)


@pytest.fixture
def clients(issuer) -> dict:
    return {name: (f"ofar-{name}", issuer.certificate(f"org-{name}")) for name in ("client1", "client2")}


def create_consent(api, issuer, clients, name: str, cpf: str) -> str:
    client_id, cert = clients[name]
    token = issuer.token(client_id, cert, "consents")
    created = api.post(f"{CONSENTS}/consents", json=consent_request(cpf), token=token, headers=gateway(cert))
    assert created.status_code == 201, created.text
    return created.json()["data"]["consentId"]


def test_the_suite_flow(api, issuer, clients, seed):
    """client_credentials creates the consent, the authorisation server authorises it with the
    customer's CPF, and the customer's token reads the accounts."""
    customer = seed["customers"][0]
    consent_id = create_consent(api, issuer, clients, "client1", customer["cpf"])
    assert api.client.post(f"/mock/consents/{consent_id}/authorise", json={"cpf": "00000000000"}).status_code == 403
    assert api.client.post(f"/mock/consents/{consent_id}/authorise", json={"cpf": customer["cpf"]}).status_code == 200
    client_id, cert = clients["client1"]
    token = issuer.token(client_id, cert, f"openid accounts consent:{consent_id}")
    listed = api.get(f"{ACCOUNTS}/accounts", token=token, headers=gateway(cert))
    assert listed.status_code == 200
    assert [a["accountId"] for a in listed.json()["data"]] == customer["accounts"]
    read = api.get(f"{CONSENTS}/consents/{consent_id}", token=issuer.token(client_id, cert, "consents"),
                   headers=gateway(cert))
    assert read.json()["data"]["status"] == "AUTHORISED"


@pytest.fixture
def authorised(api, issuer, clients, seed) -> str:
    customer = seed["customers"][0]
    consent_id = create_consent(api, issuer, clients, "client1", customer["cpf"])
    assert api.client.post(f"/mock/consents/{consent_id}/authorise", json={"cpf": customer["cpf"]}).status_code == 200
    return consent_id


def accounts(api, token: str, cert) -> int:
    return api.get(f"{ACCOUNTS}/accounts", token=token, headers=gateway(cert)).status_code


def test_a_token_is_bound_to_its_certificate(api, issuer, clients, authorised):
    """The suite's multiple-client modules: client2's certificate with client1's token."""
    client_id, cert = clients["client1"]
    token = issuer.token(client_id, cert, f"accounts consent:{authorised}")
    assert accounts(api, token, cert) == 200
    assert accounts(api, token, clients["client2"][1]) == 401
    assert accounts(api, token, None) == 401
    assert accounts(api, issuer.token(client_id, cert, f"accounts consent:{authorised}", bound=False), cert) == 401


def test_a_revoked_or_expired_token_is_refused(api, issuer, clients, authorised, clock):
    client_id, cert = clients["client1"]
    token = issuer.token(client_id, cert, f"accounts consent:{authorised}")
    assert accounts(api, token, cert) == 200
    issuer.revoked.add(token)  # a reused authorisation code: Keycloak drops the session
    assert accounts(api, token, cert) == 401
    short = issuer.token(client_id, cert, f"accounts consent:{authorised}", seconds=300)
    clock.advance(seconds=300)
    assert accounts(api, short, cert) == 401


def test_only_the_issuers_ps256_tokens(api, issuer, clients, authorised):
    client_id, cert = clients["client1"]
    scope = f"accounts consent:{authorised}"
    assert accounts(api, issuer.token(client_id, cert, scope, key=_key()), cert) == 401
    assert accounts(api, issuer.token(client_id, cert, scope, alg="RS256"), cert) == 401
    assert accounts(api, issuer.token(client_id, cert, scope, issuer="https://elsewhere/realms/ofb"), cert) == 401
    assert accounts(api, issuer.token(client_id, cert, scope, audience=["account"]), cert) == 401
    assert accounts(api, "not-a-jwt", cert) == 401
    assert accounts(api, authorised, cert) == 401  # the stand-in's bearer is not accepted here


def test_a_client_sees_only_its_own_consents(api, issuer, clients, authorised):
    other_id, other_cert = clients["client2"]
    # client2's token naming client1's consent.
    assert accounts(api, issuer.token(other_id, other_cert, f"accounts consent:{authorised}"), other_cert) == 401
    read = api.get(f"{CONSENTS}/consents/{authorised}", token=issuer.token(other_id, other_cert, "consents"),
                   headers=gateway(other_cert))
    assert read.status_code == 403
    deleted = api.delete(f"{CONSENTS}/consents/{authorised}", token=issuer.token(other_id, other_cert, "consents"),
                         headers=gateway(other_cert))
    assert deleted.status_code == 403


def test_scopes(api, issuer, clients, authorised, seed):
    client_id, cert = clients["client1"]
    # No consent:<id>, or no accounts scope.
    assert accounts(api, issuer.token(client_id, cert, "accounts"), cert) == 401
    assert accounts(api, issuer.token(client_id, cert, f"consent:{authorised}"), cert) == 403
    created = api.post(f"{CONSENTS}/consents", json=consent_request(seed["customers"][0]["cpf"]),
                       token=issuer.token(client_id, cert, "accounts"), headers=gateway(cert))
    assert created.status_code == 403


def test_revoking_the_consent_ends_its_tokens(api, issuer, clients, authorised):
    client_id, cert = clients["client1"]
    token = issuer.token(client_id, cert, f"accounts consent:{authorised}")
    assert accounts(api, token, cert) == 200
    deleted = api.delete(f"{CONSENTS}/consents/{authorised}", token=issuer.token(client_id, cert, "consents"),
                         headers=gateway(cert))
    assert deleted.status_code == 204
    assert accounts(api, token, cert) == 401


def test_without_a_verifier_the_gateway_is_refused(clock, specs, issuer, clients):
    api = CheckedClient(TestClient(create_app(seed=42, customers=5, clock=clock)), specs)
    client_id, cert = clients["client1"]
    created = api.post(f"{CONSENTS}/consents", json=consent_request("76109277673"),
                       token=issuer.token(client_id, cert, "consents"), headers=gateway(cert))
    assert created.status_code == 401


def test_the_origin_of_the_limits_is_the_certificate(api, app, issuer, clients, authorised):
    client_id, cert = clients["client1"]
    token = issuer.token(client_id, cert, f"accounts consent:{authorised}")
    assert accounts(api, token, cert) == 200
    counters = app.state.limits
    assert any(origin == "org-client1" for _, origin, _ in counters.minutes), counters.minutes
    assert any(key.receiver == "org-client1" and key.obj == authorised for key in counters.months), counters.months


def test_the_seeded_consents_belong_to_the_seed_client(clock, specs, issuer, clients):
    verifier = Verifier(issuer=ISSUER, audience=AUDIENCE, jwks=issuer.jwks, introspect=issuer.introspect, clock=clock)
    api = CheckedClient(TestClient(create_app(seed=42, customers=5, clock=clock, verifier=verifier,
                                              seed_client="ofar-client1")), specs)
    authorised = api.client.get("/mock/seed").json()["customers"][0]["consents"][0]
    for name, status in (("client1", 200), ("client2", 401)):
        client_id, cert = clients[name]
        assert accounts(api, issuer.token(client_id, cert, f"accounts consent:{authorised}"), cert) == status


@pytest.mark.parametrize("defect", ["unbound-token", "expired-token", "no-introspection", "foreign-consent"])
def test_each_defect_skips_its_check_only(defect, clock, specs, issuer, clients, seed):
    """The security layer's resource-server variants (TRANSMITTER_DEFECTS): each one accepts the
    token its check refuses, and still refuses the others."""
    verifier = Verifier(issuer=ISSUER, audience=AUDIENCE, jwks=issuer.jwks, introspect=issuer.introspect, clock=clock,
                        defects=frozenset({defect}))
    api = CheckedClient(TestClient(create_app(seed=42, customers=5, clock=clock, verifier=verifier)), specs)
    consent = create_consent(api, issuer, clients, "client1", seed["customers"][0]["cpf"])
    api.client.post(f"/mock/consents/{consent}/authorise")
    client_id, cert = clients["client1"]
    other_id, other_cert = clients["client2"]
    scope = f"accounts consent:{consent}"
    revoked = issuer.token(client_id, cert, scope)
    issuer.revoked.add(revoked)
    expired = issuer.token(client_id, cert, scope, seconds=-1)
    cases = {
        "unbound-token": accounts(api, issuer.token(client_id, cert, scope), other_cert),
        "expired-token": accounts(api, expired, cert),
        "no-introspection": accounts(api, revoked, cert),
        "foreign-consent": accounts(api, issuer.token(other_id, other_cert, scope), other_cert),
    }
    assert {k: v for k, v in cases.items()} == {k: 200 if k == defect else 401 for k in cases}
    assert accounts(api, issuer.token(client_id, cert, scope, alg="RS256"), cert) == 401


def test_unknown_defects_are_refused():
    from transmitter.security import parse_defects
    assert parse_defects("unbound-token, expired-token") == {"unbound-token", "expired-token"}
    assert parse_defects("") == frozenset()
    with pytest.raises(ValueError):
        parse_defects("no-checks-at-all")
