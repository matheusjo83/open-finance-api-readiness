"""The local PKI of the security layer: a CA of our own, the servers' TLS certificates, two
clients, each with an mTLS certificate and a signing and an encryption key, and the
transmitter as resource server, with an mTLS certificate and a signing key for its calls to the
authorisation server (token introspection).

Real Open Finance certificates come from ICP-Brasil through the Directory; here every key is
made on the spot, kept in .cache/ and in Kubernetes Secrets, and never committed. The client
certificates carry what the Brazilian certificate standard puts in the subject: the
organisation id (OU) and the software statement id (UID).
"""

import base64
import datetime
import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

NAMESPACE = "security"
# The servers, by their in-cluster Service name (gateway: the transmitter's mTLS gateway).
SERVERS = ("keycloak", "conformance", "gateway")
# client1 and client2: the suite's; walker: the contract validator's walk (stage 2b).
CLIENTS = ("client1", "client2", "walker")
RESOURCE_SERVER = "transmitter"
# Stable ids, so a client's identity does not change when its keys are made again.
ID_NAMESPACE = uuid.UUID("6f1d3c55-8a0e-4f1b-9a52-3c1d2b7e9f10")
VALIDITY = datetime.timedelta(days=30)


@dataclass(frozen=True)
class Client:
    name: str

    @property
    def client_id(self) -> str:
        return f"ofar-{self.name}"

    @property
    def organisation_id(self) -> str:
        return str(uuid.uuid5(ID_NAMESPACE, f"organisation-{self.name}"))

    @property
    def software_id(self) -> str:
        return str(uuid.uuid5(ID_NAMESPACE, f"software-{self.name}"))


def hostnames(server: str) -> list[str]:
    return [f"{server}.{NAMESPACE}.svc", f"{server}.{NAMESPACE}.svc.cluster.local", server]


def _b64(n: int) -> str:
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def jwk(key: rsa.RSAPrivateKey, kid: str, use: str, alg: str, private: bool) -> dict:
    pub = key.public_key().public_numbers()
    out = {"kty": "RSA", "kid": kid, "use": use, "alg": alg, "n": _b64(pub.n), "e": _b64(pub.e)}
    if private:
        priv = key.private_numbers()
        out |= {"d": _b64(priv.d), "p": _b64(priv.p), "q": _b64(priv.q), "dp": _b64(priv.dmp1),
                "dq": _b64(priv.dmq1), "qi": _b64(priv.iqmp)}
    return out


def _key() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _pem_key(key: rsa.RSAPrivateKey) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def _pem_cert(cert: x509.Certificate) -> bytes:
    return cert.public_bytes(serialization.Encoding.PEM)


def _issue(subject: x509.Name, key, ca_cert, ca_key, now, *, server_names=None, client=False):
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(ca_cert.subject)
               .public_key(key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + VALIDITY)
               .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(True, False, True, False, False, False, False, False, False), critical=True)
               .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False))
    if server_names:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.DNSName(n) for n in server_names]), critical=False)
        builder = builder.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
    if client:
        builder = builder.add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
    return builder.sign(ca_key, hashes.SHA256())


def build(out: Path, now: datetime.datetime | None = None) -> dict:
    """Writes the PKI to out/ and returns its manifest (also written as manifest.json)."""
    now = now or datetime.datetime.now(datetime.UTC)
    out.mkdir(parents=True, exist_ok=True)

    ca_key = _key()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COUNTRY_NAME, "BR"),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Open Finance API Readiness"),
                         x509.NameAttribute(NameOID.COMMON_NAME, "OFAR local CA")])
    ca_cert = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
               .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(minutes=5)).not_valid_after(now + VALIDITY)
               .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
               .add_extension(x509.KeyUsage(False, False, False, False, False, True, True, False, False), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
               .sign(ca_key, hashes.SHA256()))
    (out / "ca.pem").write_bytes(_pem_cert(ca_cert))

    manifest = {"ca": "ca.pem", "servers": {}, "clients": {}}
    for server in SERVERS:
        key = _key()
        names = hostnames(server)
        cert = _issue(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]), key,
                      ca_cert, ca_key, now, server_names=names)
        (out / f"{server}.pem").write_bytes(_pem_cert(cert))
        (out / f"{server}.key").write_bytes(_pem_key(key))
        manifest["servers"][server] = {"cert": f"{server}.pem", "key": f"{server}.key", "hostnames": names}

    for name in CLIENTS:
        c = Client(name)
        tls_key, sig_key, enc_key = _key(), _key(), _key()
        subject = x509.Name([
            x509.NameAttribute(NameOID.COUNTRY_NAME, "BR"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OFAR receiver"),
            x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, c.organisation_id),
            x509.NameAttribute(NameOID.USER_ID, c.software_id),
            x509.NameAttribute(NameOID.COMMON_NAME, c.client_id),
        ])
        cert = _issue(subject, tls_key, ca_cert, ca_key, now, client=True)
        (out / f"{name}.pem").write_bytes(_pem_cert(cert))
        (out / f"{name}.key").write_bytes(_pem_key(tls_key))
        keys = [(sig_key, f"{name}-sig", "sig", "PS256"), (enc_key, f"{name}-enc", "enc", "RSA-OAEP")]
        (out / f"{name}.jwks.json").write_text(json.dumps(
            {"keys": [jwk(k, kid, use, alg, private=True) for k, kid, use, alg in keys]}, indent=1))
        manifest["clients"][name] = {
            "client_id": c.client_id, "organisation_id": c.organisation_id, "software_id": c.software_id,
            "cert": f"{name}.pem", "key": f"{name}.key", "jwks": f"{name}.jwks.json",
            "public_jwks": {"keys": [jwk(k, kid, use, alg, private=False) for k, kid, use, alg in keys]},
        }
    # The resource server: a client of Keycloak's introspection endpoint (private_key_jwt, mTLS).
    rs = Client(RESOURCE_SERVER)
    tls_key, sig_key = _key(), _key()
    subject = x509.Name([x509.NameAttribute(NameOID.COUNTRY_NAME, "BR"),
                         x509.NameAttribute(NameOID.ORGANIZATION_NAME, "OFAR transmitter"),
                         x509.NameAttribute(NameOID.COMMON_NAME, rs.client_id)])
    cert = _issue(subject, tls_key, ca_cert, ca_key, now, client=True)
    name = RESOURCE_SERVER
    (out / f"{name}.pem").write_bytes(_pem_cert(cert))
    (out / f"{name}.key").write_bytes(_pem_key(tls_key))
    (out / f"{name}.jwks.json").write_text(json.dumps(
        {"keys": [jwk(sig_key, f"{name}-sig", "sig", "PS256", private=True)]}, indent=1))
    manifest["resource_server"] = {
        "client_id": rs.client_id, "cert": f"{name}.pem", "key": f"{name}.key", "jwks": f"{name}.jwks.json",
        "public_jwks": {"keys": [jwk(sig_key, f"{name}-sig", "sig", "PS256", private=False)]},
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def current(manifest: dict) -> bool:
    """Whether a kept PKI has every server and client this version needs."""
    return (set(manifest.get("servers", {})) == set(SERVERS) and set(manifest.get("clients", {})) == set(CLIENTS)
            and "resource_server" in manifest)
