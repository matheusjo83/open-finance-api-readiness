"""Client-side telemetry: one PCM report (role CLIENT) per Open Finance exchange, as the
receiving institution would send it (docs/telemetry.md).

The time runs from just before the request until the last byte of the response, before any
processing (Manual de APIs 5.3.2 V); the walk measures it. The additionalInfo follows the portal's
pages (Consentimento and Contas, Regras de Obrigatoriedade).
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .rules import Exchange
from .spec import Spec

UUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")
REPORTED_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH"}


@dataclass(frozen=True)
class Parties:
    """The two institutions of every exchange; the proxy has the same ids."""

    client_org_id: str = str(uuid.uuid5(uuid.NAMESPACE_DNS, "receiver.ofb.svc"))
    server_org_id: str = str(uuid.uuid5(uuid.NAMESPACE_DNS, "transmitter.ofb.svc"))
    client_ss_id: str = str(uuid.uuid5(uuid.NAMESPACE_DNS, "software-statement.receiver.ofb.svc"))
    # https://{host}/{prefix} in the PCM's rule, whatever the local transport is.
    endpoint_uri_prefix: str = "https://proxy.ofb.svc"
    # Local only, like the tokens it hashes.
    token_pepper: str = "local-cluster-only"


def whole_ms(seconds: float) -> int:
    """Whole milliseconds, rounded up: the portal leaves out reports with processTimespan 0."""
    return max(1, math.ceil(seconds * 1000))


def utc_ms(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def token_id(token: str, pepper: str) -> str:
    """The portal's recipe: SHA-256 of token + pepper, its first 72 bits, Base64URL."""
    return base64.urlsafe_b64encode(hashlib.sha256((token + pepper).encode()).digest()[:9]).decode()


def _document(body: bytes | None) -> Any:
    try:
        return json.loads(body) if body else None
    except ValueError:
        return None


def _data(body: bytes | None) -> dict[str, Any]:
    """The body's data object, or its first item when data is a list."""
    document = _document(body)
    data = document.get("data") if isinstance(document, dict) else None
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else {}


def _linked(data: dict[str, Any]) -> bool:
    journey = data.get("journey")
    return isinstance(journey, dict) and str(journey.get("isLinked")).lower() == "true"


def _put(info: dict[str, Any], name: str, value: Any) -> None:
    if isinstance(value, str) and value:
        info[name] = value


def additional_info(ex: Exchange, template: str, parties: Parties) -> dict[str, Any]:
    """template: the operation's path in its own API, e.g. /consents/{consentId}."""
    info: dict[str, Any] = {}
    token = ex.request_headers.get("authorization", "")[7:]
    success = ex.status < 400
    if success:
        _put(info, "tokenId", token_id(token, parties.token_pepper) if token else None)
    if not template.startswith("/consents"):
        _put(info, "consentId", token if token.startswith("urn:") else None)  # the bearer is the consent here
        return info

    data = _data(ex.body) if success else {}
    if template == "/consents":  # POST
        info["authorisationFlowIntent"] = "HYBRID_FLOW"
        info["personType"] = "PJ" if _data(ex.request_body).get("businessEntity") else "PF"
        _put(info, "consentId", data.get("consentId"))
    elif ex.method != "POST" or success:
        _put(info, "consentId", ex.path.split("/consents/")[-1].split("/")[0])
    if ex.method in ("POST", "GET"):
        if template in ("/consents", "/consents/{consentId}"):
            info["journeyIsLinked"] = "TRUE" if _linked(data) or _linked(_data(ex.request_body)) else "FALSE"
        _put(info, "expirationDateTime", data.get("expirationDateTime"))
    if ex.method == "GET" and template == "/consents/{consentId}":
        _put(info, "status", data.get("status"))
        rejection = data.get("rejection")
        if data.get("status") == "REJECTED" and isinstance(rejection, dict):
            _put(info, "rejectedBy", rejection.get("rejectedBy"))
            reason = rejection.get("reason")
            _put(info, "rejectionReasonCode", reason.get("code") if isinstance(reason, dict) else None)
    return info


def client_report(spec: Spec, ex: Exchange, parties: Parties) -> dict[str, Any] | None:
    """None when the operation is not in the specification: the PCM has no endpoint for it."""
    operation = spec.operation(ex.method, ex.path)
    if operation is None or ex.method not in REPORTED_METHODS or ex.started is None:
        return None
    report: dict[str, Any] = {
        "role": "CLIENT", "endpoint": spec.prefix + operation.template, "httpMethod": ex.method,
        "statusCode": ex.status, "timestamp": utc_ms(ex.started), "processTimespan": whole_ms(ex.seconds),
        "clientOrgId": parties.client_org_id, "serverOrgId": parties.server_org_id,
        "clientSSId": parties.client_ss_id, "endpointUriPrefix": parties.endpoint_uri_prefix,
        "additionalInfo": additional_info(ex, operation.template, parties),
    }
    if ex.interaction_id and UUID.match(ex.interaction_id):
        report["fapiInteractionId"] = ex.interaction_id
    return report


def client_record(run_id: str, walk_id: str, spec: Spec, ex: Exchange, parties: Parties) -> dict[str, Any] | None:
    report = client_report(spec, ex, parties)
    if report is None:
        return None
    return {"run_id": run_id, "walk_id": walk_id, "sent_at": utc_ms(datetime.now(timezone.utc)), "report": report}
