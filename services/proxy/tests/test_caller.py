"""The caller as the proxy sees it through the gateway (caller.py) and the draw's key."""

import base64
import datetime as dt
import json
import urllib.parse

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from defect_proxy.caller import CERT_HEADER, consent_of, is_jwt, organisation, origin
from defect_proxy.catalogue import Exchange
from defect_proxy.limits import DEFAULT_ORIGIN, ORIGIN_HEADER
from defect_proxy.selection import request_key


def jwt_of(claims: dict) -> str:
    """An unsigned JWT-shaped token: the proxy reads it without checking (the transmitter checks)."""
    part = lambda d: base64.urlsafe_b64encode(json.dumps(d).encode()).rstrip(b"=").decode()  # noqa: E731
    return f"{part({'alg': 'PS256'})}.{part(claims)}.c2ln"


def exchange(token: str | None, path: str = "/open-banking/accounts/v2/accounts") -> Exchange:
    headers = {"authorization": f"Bearer {token}"} if token else {}
    return Exchange(method="GET", path=path, route="accounts.list", query=[("page", "1")],
                    request_headers=headers, status=200, headers={}, body=None)


def test_the_consent_of_a_token():
    assert consent_of(jwt_of({"scope": "openid accounts consent:urn:transmitter:abc"})) == "urn:transmitter:abc"
    assert consent_of(jwt_of({"scope": "consents"})) is None          # client credentials
    assert consent_of("urn:transmitter:seed-1-authorised") == "urn:transmitter:seed-1-authorised"  # stand-in
    assert consent_of(None) is None
    assert is_jwt(jwt_of({})) and not is_jwt("urn:transmitter:x")


def test_the_draw_is_keyed_on_the_consent_not_the_token():
    one = jwt_of({"scope": "accounts consent:urn:transmitter:abc", "jti": "1"})
    two = jwt_of({"scope": "accounts consent:urn:transmitter:abc", "jti": "2"})
    assert request_key(42, exchange(one)) == request_key(42, exchange(two))
    other = jwt_of({"scope": "accounts consent:urn:transmitter:xyz"})
    assert request_key(42, exchange(one)) != request_key(42, exchange(other))
    # The stand-in keys as before; a repeat is a draw of its own.
    stand_in = exchange("urn:transmitter:seed-1-authorised")
    assert request_key(42, stand_in) == "42|GET|/open-banking/accounts/v2/accounts|page=1|urn:transmitter:seed-1-authorised"
    assert request_key(42, stand_in, 2) == request_key(42, stand_in) + "|2"


def certificate(organisation_id: str) -> str:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = dt.datetime.now(dt.UTC)
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, organisation_id),
                      x509.NameAttribute(NameOID.COMMON_NAME, "walker")])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=1))
            .sign(key, hashes.SHA256()))
    return urllib.parse.quote(cert.public_bytes(serialization.Encoding.PEM).decode())


def test_the_origin_is_the_certificates_organisation_then_the_stand_in():
    cert = certificate("org-walker")
    assert organisation(cert) == "org-walker" and organisation("garbage") is None
    assert origin({CERT_HEADER: cert, ORIGIN_HEADER: "stand-in"}) == "org-walker"
    assert origin({ORIGIN_HEADER: "stand-in"}) == "stand-in"
    assert origin({}) == DEFAULT_ORIGIN
