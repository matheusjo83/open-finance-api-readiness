"""Who calls, as the proxy sees it behind the transmitter's mTLS gateway (FAPI-BR stage 2b).

Through the gateway the caller's certificate comes in `x-ofar-client-cert` (URL-encoded PEM, as
nginx verified it): its organisation (OU) is the origin. Without it, the stand-in header
`x-mock-client-org-id` (the limits' k6 load), else the fixed receiver.

The bearer is a JWT of Keycloak through the gateway, or a consent id on the stand-in path. Its
consent is read without checking the token (the transmitter checks it): only to key the draw
and the labels, and to find a token of another consent for consent-not-enforced.
"""

from __future__ import annotations

import base64
import json
import urllib.parse

from cryptography import x509
from cryptography.x509.oid import NameOID

from .limits import DEFAULT_ORIGIN, ORIGIN_HEADER

CERT_HEADER = "x-ofar-client-cert"


def is_jwt(token: str | None) -> bool:
    return bool(token) and token.count(".") == 2


def claims(token: str) -> dict:
    try:
        payload = token.split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (IndexError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def consent_of(token: str | None) -> str | None:
    """The consent of `consent:<id>` in a JWT's scope (None for a client credentials token); a
    stand-in bearer is the consent itself."""
    if not is_jwt(token):
        return token
    found = [s.split(":", 1)[1] for s in str(claims(token).get("scope", "")).split() if s.startswith("consent:")]
    return found[0] if len(found) == 1 else None


def organisation(cert_header: str | None) -> str | None:
    if not cert_header:
        return None
    try:
        cert = x509.load_pem_x509_certificate(urllib.parse.unquote(cert_header).encode())
    except ValueError:
        return None
    units = cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)
    return str(units[0].value) if len(units) == 1 else None


def origin(headers) -> str:
    """`headers`: any mapping with lower-case names (or a case-insensitive one)."""
    return organisation(headers.get(CERT_HEADER)) or headers.get(ORIGIN_HEADER) or DEFAULT_ORIGIN
