"""The pieces of the real flows that can be checked without Keycloak (auth.py); the flows
themselves run in the cluster (make validate, docs/security.md)."""

import json
import os

import jwt
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding, rsa
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from contract_validator.auth import AuthError, b64url, consent_of, decrypt_jwe, forms


def encrypt(plain: str, public, enc: str = "A256GCM") -> str:
    header = b64url(json.dumps({"alg": "RSA-OAEP", "enc": enc}).encode())
    cek, iv = os.urandom(32), os.urandom(12)
    key = public.encrypt(cek, padding.OAEP(mgf=padding.MGF1(hashes.SHA1()), algorithm=hashes.SHA1(), label=None))
    sealed = AESGCM(cek).encrypt(iv, plain.encode(), header.encode())
    return ".".join([header, b64url(key), b64url(iv), b64url(sealed[:-16]), b64url(sealed[-16:])])


def test_the_id_token_is_decrypted():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    signed = jwt.encode({"nonce": "n"}, rsa.generate_private_key(public_exponent=65537, key_size=2048), algorithm="PS256")
    assert decrypt_jwe(encrypt(signed, key.public_key()), key) == signed
    with pytest.raises(AuthError, match="not encrypted"):
        decrypt_jwe(signed, key)
    with pytest.raises(AuthError, match="A128GCM"):
        decrypt_jwe(encrypt(signed, key.public_key(), enc="A128GCM"), key)


LOGIN = """<html><body><form id="kc-form-login" action="https://keycloak.security.svc/realms/ofb/login-actions/authenticate?session_code=x&amp;execution=y" method="post">
<input id="username" name="username" type="text"/><input id="password" name="password" type="password"/>
<input type="hidden" id="id-hidden-input" name="credentialId"/>
<button name="login" id="kc-login" type="submit">Sign In</button></form></body></html>"""
GRANT = """<form class="form-actions" action="/realms/ofb/login-actions/required-action?execution=OAUTH_GRANT" method="POST">
<input type="hidden" name="code" value="abc"/><input name="accept" id="kc-login" type="submit" value="Yes"/>
<input name="cancel" id="kc-cancel" type="submit" value="No"/></form>"""


def test_the_login_and_grant_forms_are_read():
    (login,) = forms(LOGIN)
    assert login["action"].endswith("session_code=x&execution=y")
    assert {"username", "password", "credentialId"} <= set(login["fields"]) and "login" in login["buttons"]
    (grant,) = forms(GRANT)
    assert grant["fields"] == {"code": "abc"} and grant["buttons"] == {"accept": "Yes", "cancel": "No"}


def test_the_consent_of_a_token():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert consent_of(jwt.encode({"scope": "openid accounts consent:urn:transmitter:a"}, key, algorithm="PS256")) == "urn:transmitter:a"
    assert consent_of(jwt.encode({"scope": "consents"}, key, algorithm="PS256")) is None
    assert consent_of("urn:transmitter:seed-1-authorised") == "urn:transmitter:seed-1-authorised"
    assert consent_of(None) is None
