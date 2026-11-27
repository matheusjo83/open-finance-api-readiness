"""Server-side telemetry: one PCM report (role SERVER) per Open Finance exchange, as the gateway
of the transmitting institution would measure it (docs/telemetry.md).

The time runs from the request's arrival until the last byte of the response has been sent
(Manual de APIs 5.3), so it is an ASGI middleware around the whole proxy. The reports follow the
PCM specification pinned by `make specs`, with the domains of the portal's pages.
"""

from __future__ import annotations

import asyncio
import math
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from .catalogue import ACCOUNTS, CONSENTS, route_of
from .labels import LabelSink

# Route -> the endpoint as the PCM accepts it (a template, never the real path).
ENDPOINTS = {
    "accounts.list": f"{ACCOUNTS}/accounts",
    "accounts.detail": f"{ACCOUNTS}/accounts/{{accountId}}",
    "accounts.balances": f"{ACCOUNTS}/accounts/{{accountId}}/balances",
    "accounts.transactions": f"{ACCOUNTS}/accounts/{{accountId}}/transactions",
    "accounts.transactions-current": f"{ACCOUNTS}/accounts/{{accountId}}/transactions-current",
    "accounts.overdraft-limits": f"{ACCOUNTS}/accounts/{{accountId}}/overdraft-limits",
    "consents.create": f"{CONSENTS}/consents",
    "consents.read": f"{CONSENTS}/consents/{{consentId}}",
    "consents.delete": f"{CONSENTS}/consents/{{consentId}}",
    "consents.extend": f"{CONSENTS}/consents/{{consentId}}/extends",
    "consents.extensions": f"{CONSENTS}/consents/{{consentId}}/extensions",
}
UUID = re.compile(r"^[0-9a-fA-F]{8}-([0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}$")


@dataclass(frozen=True)
class Parties:
    """The two institutions of every exchange. A real server knows the client from its
    certificate (the FAPI layer, later); here both are settings, equal on both sides."""

    client_org_id: str = str(uuid.uuid5(uuid.NAMESPACE_DNS, "receiver.ofb.svc"))
    server_org_id: str = str(uuid.uuid5(uuid.NAMESPACE_DNS, "transmitter.ofb.svc"))


def whole_ms(seconds: float) -> int:
    """Whole milliseconds, rounded up: the portal leaves out reports with processTimespan 0."""
    return max(1, math.ceil(seconds * 1000))


def utc_ms(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def drop_reason(status: int) -> str:
    """Whether the customer could go on (POST /consents): no credential check happens here, so
    every created consent is NONE."""
    return "NONE" if status == 201 else "CREDENTIAL_UNAVAILABLE" if status >= 500 else "NO_CREDENTIAL"


def server_report(route: str, method: str, status: int, interaction_id: str | None, started: datetime,
                  seconds: float, parties: Parties, client_org_id: str | None = None) -> dict[str, Any]:
    """`client_org_id`: the caller's x-mock-client-org-id (the stand-in for its certificate), or
    the fixed receiver of `parties`."""
    report: dict[str, Any] = {
        "role": "SERVER", "endpoint": ENDPOINTS[route], "httpMethod": method, "statusCode": status,
        "timestamp": utc_ms(started), "processTimespan": whole_ms(seconds),
        "clientOrgId": client_org_id or parties.client_org_id, "serverOrgId": parties.server_org_id,
        "additionalInfo": {"dropReason": drop_reason(status)} if route == "consents.create" else {},
    }
    if interaction_id and UUID.match(interaction_id):
        report["fapiInteractionId"] = interaction_id
    return report


def _header(headers: list[tuple[bytes, bytes]], name: bytes) -> str | None:
    return next((v.decode("latin-1") for k, v in headers if k.lower() == name), None)


class TelemetryMiddleware:
    """Times every exchange on a known route and buffers its report in the sink."""

    def __init__(self, app, sink: LabelSink, parties: Parties) -> None:
        self.app, self.sink, self.parties = app, sink, parties

    async def __call__(self, scope, receive, send) -> None:
        route = route_of(scope["method"], scope["path"]) if scope["type"] == "http" else None
        if route is None:
            await self.app(scope, receive, send)
            return
        started, t0 = datetime.now(timezone.utc), time.perf_counter()
        status, headers, finished = 500, [], False  # 500: what an unhandled error answers

        def finish() -> None:
            nonlocal finished
            if finished:
                return
            finished = True
            seconds = time.perf_counter() - t0
            # The client's id, or the one the server generated when the client sent none.
            interaction = _header(scope["headers"], b"x-fapi-interaction-id") or _header(headers, b"x-fapi-interaction-id")
            report = server_report(route, scope["method"], status, interaction, started, seconds, self.parties,
                                   _header(scope["headers"], b"x-mock-client-org-id"))
            if self.sink.add({"run_id": self.sink.run_id, "sent_at": utc_ms(datetime.now(timezone.utc)), "report": report}):
                asyncio.create_task(self.sink.aflush())

        async def timed(message) -> None:
            nonlocal status, headers
            if message["type"] == "http.response.start":
                status, headers = message["status"], list(message.get("headers", []))
            await send(message)
            if message["type"] == "http.response.body" and not message.get("more_body"):
                finish()

        try:
            await self.app(scope, receive, timed)
        finally:
            finish()
