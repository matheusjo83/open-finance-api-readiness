"""The defects of the security layer (backlog item 19, stage 3; decision 51): one variant per run,
each breaking one requirement of FAPI-BR v2.2.0, at the authorisation server (a patch of the
clean realm) or at the resource server (a flag of the transmitter, TRANSMITTER_DEFECTS).

The unit is the run: the label is the variant, and the suite detects it when at least one module
does other than the clean baseline (an `unexpected` outcome: suite.KNOWN fixes the baseline's).
Which modules do is measured, not assumed (make variants); a variant no module catches is a gap
of the suite, for the detection report (stage 4) to show.
"""

from __future__ import annotations

import copy
from collections.abc import Callable
from dataclasses import dataclass

from . import realm as realm_module


@dataclass(frozen=True)
class Variant:
    id: str
    layer: str  # authorisation-server | resource-server
    requirement: str  # where v2.2.0 (portal page 1675395195 v2) asks for what the variant breaks
    description: str
    patch: Callable[[dict], None] | None = None  # the realm, in place (authorisation server)
    transmitter: str | None = None  # the transmitter's defect flag (resource server)


def _receivers(r: dict) -> list[dict]:
    return [c for c in r["clients"] if c["clientId"] != realm_module.RESOURCE_SERVER_CLIENT]


def _client_attributes(**changes: str | None) -> Callable[[dict], None]:
    def patch(r: dict) -> None:
        for c in _receivers(r):
            for key, value in changes.items():
                if value is None:
                    c["attributes"].pop(key, None)
                else:
                    c["attributes"][key] = value
    return patch


def _without_executor(name: str) -> Callable[[dict], None]:
    def patch(r: dict) -> None:
        for profile in r["clientProfiles"]["profiles"]:
            profile["executors"] = [e for e in profile["executors"] if e["executor"] != name]
    return patch


def _both(*patches: Callable[[dict], None]) -> Callable[[dict], None]:
    def patch(r: dict) -> None:
        for p in patches:
            p(r)
    return patch


def _realm(**changes) -> Callable[[dict], None]:
    def patch(r: dict) -> None:
        for key, value in changes.items():
            if key == "attributes":
                r["attributes"].update(value)
            else:
                r[key] = value
    return patch


def _rs256_key(r: dict) -> None:
    r["components"]["org.keycloak.keys.KeyProvider"].append(
        {"name": "rs256", "providerId": "rsa-generated",
         "config": {"keySize": ["2048"], "active": ["true"], "enabled": ["true"], "priority": ["90"],
                    "algorithm": ["RS256"]}})


AS, RS = "authorisation-server", "resource-server"
VARIANTS = {v.id: v for v in (
    Variant("par-optional", AS, "5.1 item 3: PAR is required",
            "The authorisation endpoint also takes a request object by value, without PAR",
            patch=_client_attributes(**{"require.pushed.authorization.requests": "false"})),
    Variant("pkce-optional", AS, "5.1 item 18: PKCE with S256",
            "PKCE is not required (no pkce-enforcer, no method on the clients)",
            patch=_both(_without_executor("pkce-enforcer"), _client_attributes(**{"pkce.code.challenge.method": None}))),
    Variant("id-token-rs256", AS, "6.2: PS256 for every JWS",
            "The id_token is signed with RS256",
            patch=_both(_rs256_key, _client_attributes(**{"id.token.signed.response.alg": "RS256"}))),
    Variant("long-access-token", AS, "5.1 item 11: access tokens of 300 to 900 seconds",
            "Access tokens live 1800 seconds", patch=_realm(accessTokenLifespan=1800)),
    Variant("refresh-rotation", AS, "5.1 item 13: refresh tokens are not rotated",
            "Each refresh returns a new refresh token and revokes the old one",
            patch=_both(_without_executor("suppress-refresh-token-rotation"), _realm(revokeRefreshToken=True))),
    Variant("id-token-unencrypted", AS, "5.1.1 and 6.3: the id_token encrypted (RSA-OAEP, A256GCM)",
            "The id_token is only signed",
            patch=_client_attributes(**{"id.token.encrypted.response.alg": None, "id.token.encrypted.response.enc": None})),
    Variant("request-uri-60s", AS, "5.1 item 16: a request_uri lives at least 120 seconds",
            "The request_uri expires after 60 seconds (Keycloak's default)",
            patch=_realm(attributes={"parRequestUriLifespan": "60"})),
    Variant("unbound-token", RS, "FAPI 1 Advanced 6.2.1-1 (via v2.2.0 5.1): tokens bound to the client's certificate",
            "The transmitter accepts a token with another client's certificate", transmitter="unbound-token"),
    Variant("expired-token", RS, "RFC 6750 3.1 (via v2.2.0 5.1 item 11): an expired token is refused",
            "The transmitter accepts an expired token", transmitter="expired-token"),
    Variant("foreign-consent", RS, "7: the token's consent:<id> of the client's own consent",
            "The transmitter serves a client the consents of another client", transmitter="foreign-consent"),
    Variant("no-introspection", RS, "FAPI 1 Advanced via RFC 6749 4.1.2: a revoked token is refused",
            "The transmitter does not ask Keycloak whether a token is still active", transmitter="no-introspection"),
)}


def apply(clean: dict, variant: str | None) -> tuple[dict, str]:
    """The realm for `variant` (the clean one for None) and the transmitter's defect flags."""
    if variant is None:
        return clean, ""
    v = VARIANTS[variant]
    patched = copy.deepcopy(clean)
    if v.patch is not None:
        v.patch(patched)
    return patched, v.transmitter or ""


def label(variant: str | None) -> dict:
    if variant is None:
        return {"variant": None, "layer": None, "requirement": None}
    v = VARIANTS[variant]
    return {"variant": v.id, "layer": v.layer, "requirement": v.requirement, "description": v.description}
