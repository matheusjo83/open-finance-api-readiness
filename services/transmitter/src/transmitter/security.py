"""The transmitter as resource server (FAPI-BR v2.2.0, backlog item 19 stage 2).

Requests that come through the mTLS gateway (infra/security/gateway.conf) carry the gateway's
marker and the client's certificate as nginx verified it. For those, the bearer token must be an
access token of the transmitter's authorisation server (Keycloak, realm ofb):

- a JWT signed with PS256 by a key of the issuer's JWKS, issued by it, for the transmitter (its
  client id in `aud`), not expired;
- bound to the certificate the request came with (`cnf.x5t#S256`, RFC 8705 3.1);
- still active at the authorisation server (token introspection, RFC 7662), so a token that
  Keycloak revoked (a reused authorisation code, a logout) is refused at once.

The caller's organisation is the OU of its certificate's subject, as in the Brazilian
certificate standard; it replaces the `x-mock-client-org-id` stand-in on these requests.

Requests without the gateway's marker keep the stand-ins of docs/transmitter.md until the walk
moves to the real flows (stage 2b).
"""

from __future__ import annotations

import base64
import hashlib
import json
import ssl
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import jwt
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID

# Set by the gateway on every request it forwards (any value the client sent is replaced).
GATEWAY_HEADER = "x-ofar-gateway"
# The client's certificate as nginx verified it ($ssl_client_escaped_cert: URL-encoded PEM).
CERT_HEADER = "x-ofar-client-cert"
ALGORITHM = "PS256"


class Refused(Exception):
    """The token or the certificate is not acceptable: a 401 for the caller."""


@dataclass(frozen=True)
class Caller:
    """Who called, from a checked token and the certificate it is bound to."""

    client_id: str
    organisation_id: str
    scopes: frozenset[str]
    subject: str | None

    @property
    def consent_id(self) -> str | None:
        """The consent of `consent:<id>` (v2.2.0 section 7), if the token carries one."""
        found = [s.split(":", 1)[1] for s in self.scopes if s.startswith("consent:")]
        return found[0] if len(found) == 1 else None


def thumbprint(cert: x509.Certificate) -> str:
    """x5t#S256: base64url of the SHA-256 of the certificate's DER, without padding."""
    der = cert.public_bytes(serialization.Encoding.DER)
    return base64.urlsafe_b64encode(hashlib.sha256(der).digest()).rstrip(b"=").decode()


def organisation_of(cert: x509.Certificate) -> str:
    units = cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)
    if len(units) != 1:
        raise Refused("the certificate has no single organisation (OU)")
    return str(units[0].value)


def certificate(header: str | None) -> x509.Certificate:
    if not header:
        raise Refused("no client certificate")
    try:
        return x509.load_pem_x509_certificate(urllib.parse.unquote(header).encode())
    except ValueError:
        raise Refused("the client certificate cannot be read") from None


def unverified_consent(token: str) -> str | None:
    """The consent a token names, without checking it (only to key the monthly limits, which
    count successful responses: a token refused later is never counted)."""
    try:
        scope = jwt.decode(token, options={"verify_signature": False}).get("scope", "")
    except jwt.PyJWTError:
        return None
    found = [s.split(":", 1)[1] for s in str(scope).split() if s.startswith("consent:")]
    return found[0] if len(found) == 1 else None


@dataclass
class Settings:
    """From the environment: TRANSMITTER_ISSUER, TRANSMITTER_CLIENT_ID (the resource server's
    client at the issuer) and the files of TRANSMITTER_SECURITY_DIR (Secret transmitter-security,
    made by make security-deploy): ca.pem, the resource server's mTLS certificate and key
    (transmitter.pem, transmitter.key) and its signing key (transmitter.jwks.json)."""

    issuer: str
    client_id: str
    directory: Path

    def ready(self) -> bool:
        return all((self.directory / name).exists() for name in
                   ("ca.pem", "transmitter.pem", "transmitter.key", "transmitter.jwks.json"))


@dataclass
class KeycloakClient:
    """The issuer's JWKS and token introspection, over mTLS with private_key_jwt (the realm's
    client policy applies to the resource server's client too)."""

    settings: Settings
    _context: ssl.SSLContext | None = None

    def context(self) -> ssl.SSLContext:
        if not self.settings.ready():
            raise Refused("the resource server has no credentials yet")
        if self._context is None:
            d = self.settings.directory
            ctx = ssl.create_default_context(cafile=str(d / "ca.pem"))
            ctx.load_cert_chain(str(d / "transmitter.pem"), str(d / "transmitter.key"))
            self._context = ctx
        return self._context

    def _get(self, url: str, data: bytes | None = None) -> dict:
        request = urllib.request.Request(url, data=data)
        if data is not None:
            request.add_header("content-type", "application/x-www-form-urlencoded")
        with urllib.request.urlopen(request, context=self.context(), timeout=10) as response:
            return json.loads(response.read())

    def jwks(self) -> dict:
        return self._get(f"{self.settings.issuer}/protocol/openid-connect/certs")

    def introspect(self, token: str) -> dict:
        d = self.settings.directory
        client_id = self.settings.client_id
        key = json.loads((d / "transmitter.jwks.json").read_text())["keys"][0]
        endpoint = f"{self.settings.issuer}/protocol/openid-connect/token/introspect"
        now = int(time.time())
        assertion = jwt.encode(
            {"iss": client_id, "sub": client_id, "aud": self.settings.issuer, "jti": str(uuid.uuid4()),
             "iat": now, "exp": now + 60},
            jwt.PyJWK(key).key, algorithm=ALGORITHM, headers={"kid": key["kid"]})
        form = urllib.parse.urlencode({
            "token": token, "client_id": client_id,
            "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-bearer",
            "client_assertion": assertion}).encode()
        return self._get(endpoint, form)


def reach(call: Callable, *args) -> dict:
    """A call to the authorisation server; when it cannot be reached, nothing can be checked."""
    try:
        return call(*args)
    except (OSError, ValueError, KeyError) as exc:
        raise Refused(f"the authorisation server cannot be reached: {exc}") from None


@dataclass
class Verifier:
    issuer: str
    audience: str
    jwks: Callable[[], dict]
    introspect: Callable[[str], dict]
    clock: Callable[[], datetime]
    _keys: dict[str, object] = field(default_factory=dict)

    def key(self, kid: str | None):
        if kid not in self._keys:
            # An unknown kid: the issuer may have rotated its keys.
            self._keys = {k["kid"]: jwt.PyJWK(k).key for k in reach(self.jwks)["keys"]
                          if k.get("use", "sig") == "sig" and k.get("alg") == ALGORITHM}
        if kid not in self._keys:
            raise Refused("the token is not signed by a key of the issuer")
        return self._keys[kid]

    def check(self, token: str, cert_header: str | None) -> Caller:
        cert = certificate(cert_header)
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError:
            raise Refused("the token is not a JWT") from None
        if header.get("alg") != ALGORITHM:
            raise Refused(f"the token is not signed with {ALGORITHM}")
        try:
            claims = jwt.decode(token, self.key(header.get("kid")), algorithms=[ALGORITHM], issuer=self.issuer,
                                audience=self.audience,
                                options={"verify_exp": False, "require": ["exp", "iss", "aud"]})
        except jwt.PyJWTError as exc:
            raise Refused(f"the token does not verify: {exc}") from None
        if self.clock().timestamp() >= claims["exp"]:
            raise Refused("the token has expired")
        bound = (claims.get("cnf") or {}).get("x5t#S256")
        if not bound or bound != thumbprint(cert):
            raise Refused("the token is not bound to the client's certificate")
        if not reach(self.introspect, token).get("active"):
            raise Refused("the token is no longer active")
        return Caller(client_id=str(claims.get("azp") or claims.get("client_id") or ""),
                      organisation_id=organisation_of(cert),
                      scopes=frozenset(str(claims.get("scope", "")).split()),
                      subject=claims.get("sub"))
