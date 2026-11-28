"""How the walk gets its tokens (FAPI-BR stage 2b, decision 50).

`StandIn`: the transmitter's stand-ins, as in the tests in process: the consent id is the
customer's token, any string the client credentials token, POST /mock/.../authorise the
customer's approval, and the run's receiver in `x-mock-client-org-id`.

`Keycloak`: the real flows of FAPI-BR v2.2.0 against the transmitter's authorisation server,
over mTLS with the walk's certificate (the receiver is its organisation):
- client credentials with private_key_jwt (PS256), scope `consents`;
- per consent, the customer's authorisation: a signed request object pushed (PAR) with PKCE,
  the authorisation endpoint with the request_uri, the customer's login and the grant screen
  (scripted, as the suite's browser does), the code of the hybrid response (`code id_token`)
  exchanged with the code verifier, and both id_tokens decrypted (RSA-OAEP, A256GCM) and checked
  (PS256 by the issuer, audience, nonce, acr loa2). Keycloak approves the consent at the
  transmitter as it issues the token (infra/security/authorise-consent.js);
- tokens kept per consent until 30 s before they expire, then refreshed.
Each seeded customer is a user of the realm, created from GET /mock/seed with Keycloak's admin
API before the walk.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import ssl
import time
import uuid
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit

import httpx2
import jwt
from jwt.algorithms import RSAAlgorithm
from cryptography import x509
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives import hashes
from cryptography.x509.oid import NameOID

from .telemetry import run_receiver

# The client credentials tokens of the stand-in path: the transmitter accepts any bearer there.
CLIENT_TOKEN = "validator-client-credentials"
CLIENT_TOKEN_RENEWED = "validator-client-credentials-renewed"
LOA2 = "urn:brasil:openbanking:loa2"
ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"


class AuthError(RuntimeError):
    """The authorisation server did not give what the clean flow should: the walk stops."""


class StandIn:
    def __init__(self, client) -> None:
        self.client, self.origin = client, None

    # A token for a consent nobody approved: the stand-in hands one out (the consent id).
    tokens_for_unapproved = True

    def receiver(self, run_id: str) -> str:
        self.origin = run_receiver(run_id)
        return self.origin

    def headers(self) -> dict[str, str]:
        return {"x-mock-client-org-id": self.origin} if self.origin else {}

    def prepare(self, seed: dict) -> None:
        pass

    def client_token(self, renewed: bool = False) -> str:
        return CLIENT_TOKEN_RENEWED if renewed else CLIENT_TOKEN

    def consent_token(self, consent: str, cpf: str) -> str:
        return consent

    def authorise(self, consent: str, cpf: str) -> None:
        self.client.request("POST", f"/mock/consents/{consent}/authorise", headers={})


def consent_of(token: str | None) -> str | None:
    """The consent of a JWT's `consent:<id>` (None for a client credentials token), read
    without checking; a stand-in bearer is the consent itself."""
    if not token or token.count(".") != 2:
        return token
    try:
        scope = jwt.decode(token, options={"verify_signature": False}).get("scope", "")
    except jwt.PyJWTError:
        return None
    found = [s.split(":", 1)[1] for s in str(scope).split() if s.startswith("consent:")]
    return found[0] if len(found) == 1 else None


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def unb64url(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def decrypt_jwe(token: str, key) -> str:
    """A compact JWE with RSA-OAEP (SHA-1, RFC 7518 4.3) and A256GCM: the nested JWS."""
    parts = token.split(".")
    if len(parts) != 5:
        raise AuthError("the id_token is not encrypted (FAPI-BR v2.2.0 5.1.1)")
    header = json.loads(unb64url(parts[0]))
    if (header.get("alg"), header.get("enc")) != ("RSA-OAEP", "A256GCM"):
        raise AuthError(f"the id_token is encrypted with {header.get('alg')}/{header.get('enc')}")
    cek = key.decrypt(unb64url(parts[1]), padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
    plain = AESGCM(cek).decrypt(unb64url(parts[2]), unb64url(parts[3]) + unb64url(parts[4]), parts[0].encode())
    return plain.decode()


class _Forms(HTMLParser):
    """The forms of a page: action, and the inputs and buttons with a name."""

    def __init__(self) -> None:
        super().__init__()
        self.forms: list[dict[str, Any]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self.forms.append({"action": a.get("action", ""), "fields": {}, "buttons": {}})
        elif self.forms and tag in ("input", "button") and a.get("name"):
            kind = a.get("type", "submit" if tag == "button" else "text")
            target = self.forms[-1]["buttons" if kind == "submit" else "fields"]
            target[a["name"]] = a.get("value", "")


def forms(html: str) -> list[dict[str, Any]]:
    parser = _Forms()
    parser.feed(html)
    return parser.forms


@dataclass
class Grant:
    access: str
    expires: float
    refresh: str | None


@dataclass
class Keycloak:
    """`directory`: the Secret walker-security (make auth-deploy): ca.pem, walker.pem,
    walker.key, walker.jwks.json, keycloak-admin-password, walker-user-password."""

    issuer: str
    client_id: str
    directory: Path
    redirect_uri: str = "https://receiver.ofb.svc/callback"
    tokens_for_unapproved = False  # only the customer's approval gets a consent a token
    _client: httpx2.Client | None = None
    _endpoints: dict[str, str] = field(default_factory=dict)
    _keys: dict[str, Any] = field(default_factory=dict)
    _grants: dict[str, Grant] = field(default_factory=dict)
    _client_grant: Grant | None = None
    logins: int = 0

    # --- plumbing ------------------------------------------------------------------------

    def context(self) -> ssl.SSLContext:
        ctx = ssl.create_default_context(cafile=str(self.directory / "ca.pem"))
        ctx.load_cert_chain(str(self.directory / "walker.pem"), str(self.directory / "walker.key"))
        return ctx

    def http(self) -> httpx2.Client:
        if self._client is None:
            self._client = httpx2.Client(verify=self.context(), timeout=15.0)
        return self._client

    def secret(self, name: str) -> str:
        return (self.directory / name).read_text().strip()

    def jwk(self, use: str):
        entry = next(k for k in json.loads((self.directory / "walker.jwks.json").read_text())["keys"] if k["use"] == use)
        # The RSA key whatever its alg (PyJWT knows no RSA-OAEP).
        return entry["kid"], RSAAlgorithm.from_jwk(entry)

    def endpoint(self, name: str) -> str:
        if not self._endpoints:
            self._endpoints = self.http().get(f"{self.issuer}/.well-known/openid-configuration").raise_for_status().json()
        return self._endpoints[name]

    def assertion(self) -> str:
        kid, key = self.jwk("sig")
        now = int(time.time())
        return jwt.encode({"iss": self.client_id, "sub": self.client_id, "aud": self.issuer, "jti": str(uuid.uuid4()),
                           "iat": now, "exp": now + 60}, key, algorithm="PS256", headers={"kid": kid})

    def client_auth(self) -> dict[str, str]:
        return {"client_id": self.client_id, "client_assertion_type": ASSERTION_TYPE, "client_assertion": self.assertion()}

    def token_request(self, form: dict[str, str]) -> dict:
        response = self.http().post(self.endpoint("token_endpoint"), data={**form, **self.client_auth()})
        if response.status_code != 200:
            raise AuthError(f"token endpoint: {response.status_code} {response.text[:300]}")
        return response.json()

    def issuer_key(self, kid: str | None):
        if kid not in self._keys:
            jwks = self.http().get(self.endpoint("jwks_uri")).raise_for_status().json()
            self._keys = {k["kid"]: jwt.PyJWK(k).key for k in jwks["keys"] if k.get("use") == "sig" and k.get("alg") == "PS256"}
        if kid not in self._keys:
            raise AuthError("the id_token is not signed by a key of the issuer")
        return self._keys[kid]

    def id_token(self, token: str, nonce: str) -> dict:
        _, enc = self.jwk("enc")
        signed = decrypt_jwe(token, enc)
        try:
            claims = jwt.decode(signed, self.issuer_key(jwt.get_unverified_header(signed).get("kid")),
                                algorithms=["PS256"], audience=self.client_id, issuer=self.issuer)
        except jwt.PyJWTError as exc:
            raise AuthError(f"the id_token does not verify: {exc}") from None
        if claims.get("nonce") != nonce or claims.get("acr") != LOA2:
            raise AuthError(f"the id_token's nonce or acr is wrong: {claims.get('acr')}")
        return claims

    # --- the walk's interface ------------------------------------------------------------

    def receiver(self, run_id: str) -> str:
        cert = x509.load_pem_x509_certificate((self.directory / "walker.pem").read_bytes())
        return str(cert.subject.get_attributes_for_oid(NameOID.ORGANIZATIONAL_UNIT_NAME)[0].value)

    def headers(self) -> dict[str, str]:
        return {}

    def username(self, cpf: str) -> str:
        return f"customer-{cpf}"

    def prepare(self, seed: dict) -> None:
        """Each seeded customer as a user of the realm, with its CPF (Keycloak's admin API;
        existing users are left as they are)."""
        base = self.issuer.split("/realms/")[0]
        admin = self.http().post(f"{base}/realms/master/protocol/openid-connect/token", data={
            "grant_type": "password", "client_id": "admin-cli", "username": "admin",
            "password": self.secret("keycloak-admin-password")}).raise_for_status().json()["access_token"]
        realm = self.issuer.split("/realms/")[1]
        for customer in seed["customers"]:
            cpf = customer["cpf"]
            response = self.http().post(f"{base}/admin/realms/{realm}/users", headers={"authorization": f"Bearer {admin}"}, json={
                "username": self.username(cpf), "enabled": True, "email": f"{cpf}@example.com", "emailVerified": True,
                "firstName": "Customer", "lastName": cpf, "attributes": {"cpf": [cpf]},
                "credentials": [{"type": "password", "value": self.secret("walker-user-password"), "temporary": False}]})
            if response.status_code not in (201, 409):
                raise AuthError(f"user {cpf}: {response.status_code} {response.text[:200]}")

    def client_token(self, renewed: bool = False) -> str:
        grant = self._client_grant
        if renewed or grant is None or grant.expires - time.time() < 30:
            body = self.token_request({"grant_type": "client_credentials", "scope": "consents"})
            grant = self._client_grant = Grant(body["access_token"], time.time() + body["expires_in"], None)
        return grant.access

    def consent_token(self, consent: str, cpf: str) -> str:
        grant = self._grants.get(consent)
        if grant is not None and grant.expires - time.time() < 30 and grant.refresh:
            body = self.token_request({"grant_type": "refresh_token", "refresh_token": grant.refresh})
            grant = self._grants[consent] = Grant(body["access_token"], time.time() + body["expires_in"],
                                                  body.get("refresh_token", grant.refresh))
        if grant is None or grant.expires - time.time() < 30:
            self.authorise(consent, cpf)
            grant = self._grants[consent]
        return grant.access

    def authorise(self, consent: str, cpf: str) -> None:
        """The customer's authorisation of `consent`: PAR, login, grant, code for tokens."""
        verifier, state, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(16), secrets.token_urlsafe(16)
        scope = f"openid accounts consent:{consent}"
        kid, key = self.jwk("sig")
        now = int(time.time())
        request_object = jwt.encode({
            "iss": self.client_id, "aud": self.issuer, "client_id": self.client_id, "jti": str(uuid.uuid4()),
            "iat": now, "nbf": now, "exp": now + 300, "response_type": "code id_token",
            "redirect_uri": self.redirect_uri, "scope": scope, "state": state, "nonce": nonce,
            "code_challenge": b64url(hashlib.sha256(verifier.encode()).digest()), "code_challenge_method": "S256",
            "claims": {"id_token": {"acr": {"essential": True, "values": [LOA2]}}},
        }, key, algorithm="PS256", headers={"kid": kid})
        par = self.http().post(self.endpoint("pushed_authorization_request_endpoint"),
                               data={**self.client_auth(), "request": request_object})
        if par.status_code != 201:
            raise AuthError(f"PAR: {par.status_code} {par.text[:300]}")
        fragment = self.browse(par.json()["request_uri"], cpf)
        if fragment.get("state") != state or "code" not in fragment:
            raise AuthError(f"authorisation response without code or with another state: {sorted(fragment)}")
        self.id_token(fragment["id_token"], nonce)
        body = self.token_request({"grant_type": "authorization_code", "code": fragment["code"],
                                   "redirect_uri": self.redirect_uri, "code_verifier": verifier})
        self.id_token(body["id_token"], nonce)
        self._grants[consent] = Grant(body["access_token"], time.time() + body["expires_in"], body.get("refresh_token"))
        self.logins += 1

    def browse(self, request_uri: str, cpf: str) -> dict[str, str]:
        """The customer's browser: the authorisation endpoint, the login form, the grant screen,
        until Keycloak redirects to the walk's redirect URI (its fragment is returned)."""
        with httpx2.Client(verify=self.context(), timeout=15.0, follow_redirects=False) as browser:
            response = browser.get(self.endpoint("authorization_endpoint"),
                                   params={"client_id": self.client_id, "request_uri": request_uri})
            for _ in range(8):
                if response.status_code in (302, 303):
                    location = urljoin(str(response.url), response.headers["location"])
                    if location.startswith(self.redirect_uri):
                        parts = urlsplit(location)
                        return dict(parse_qsl(parts.fragment or parts.query))
                    response = browser.get(location)
                    continue
                if response.status_code != 200:
                    raise AuthError(f"authorisation: {response.status_code} at {response.url}")
                page = next((f for f in forms(response.text) if f["action"]), None)
                if page is None:
                    raise AuthError(f"authorisation: a page without a form at {response.url}")
                data = dict(page["fields"])
                if "password" in data:
                    data.update(username=self.username(cpf), password=self.secret("walker-user-password"))
                    data.update({k: v for k, v in page["buttons"].items() if k == "login"})
                elif "accept" in page["buttons"]:
                    data["accept"] = page["buttons"]["accept"]
                else:
                    raise AuthError(f"authorisation: an unexpected form at {response.url}: {sorted(page['buttons'])}")
                response = browser.post(urljoin(str(response.url), page["action"]), data=data)
            raise AuthError("authorisation: too many steps")
