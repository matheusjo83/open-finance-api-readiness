"""The PCM reports of both sides (docs/telemetry.md): valid against the pinned PCM specification
but for its known disagreements with the portal, paired one to one, and timed."""

import asyncio
import json
import re
import time
from datetime import datetime, timezone

import httpx2
import pytest
import yaml
from openapi_schema_validator import OAS30Validator, oas30_format_checker
from starlette.applications import Starlette
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from contract_validator.rules import Exchange
from contract_validator.spec import spec_for
from contract_validator.telemetry import Parties as ClientParties
from contract_validator.telemetry import client_report, token_id, whole_ms
from contract_validator.walk import Walker
from defect_proxy.catalogue import ROUTES
from defect_proxy.labels import LabelSink
from defect_proxy.labels import MemoryStore as LabelStore
from defect_proxy.selection import parse_rates
from defect_proxy.telemetry import ENDPOINTS, Parties as ServerParties
from defect_proxy.telemetry import TelemetryMiddleware, server_report
from defect_proxy.telemetry import whole_ms as server_whole_ms

from .conftest import REPO, SPECS, zero_rates

REQUIREMENTS = json.loads((REPO / "specs/operational/requirements.json").read_text())
PAIRING_FIELDS = REQUIREMENTS["pcm_reporting"]["pairing_fields"]
# Where the pinned PCM file disagrees with the portal's pages, which the reports follow
# (docs/telemetry.md): the only schema errors accepted, and only for these values.
KNOWN_SCHEMA_ERRORS = {
    ("ClientReportRequest", "additionalInfo.rejectedBy", "USER"),
    ("ClientReportRequest", "additionalInfo.rejectedBy", "ASPSP"),
    ("ClientReportRequest", "additionalInfo.rejectedBy", "TPP"),
    ("ServerReportRequest", "additionalInfo.dropReason", "NONE"),
}
SCHEMA_OF = {"CLIENT": "ClientReportRequest", "SERVER": "ServerReportRequest"}


@pytest.fixture(scope="session")
def pcm():
    path = SPECS / "pcm.yml"
    if not path.exists():
        pytest.exit(f"{path} not found: run `make specs` at the repository root", returncode=2)
    schemas = yaml.safe_load(path.read_text(encoding="utf-8-sig"))["components"]["schemas"]
    return {name: OAS30Validator(schemas[name], format_checker=oas30_format_checker) for name in SCHEMA_OF.values()}


def schema_errors(pcm, report: dict) -> set[tuple]:
    name = SCHEMA_OF[report["role"]]
    return {(name, ".".join(map(str, e.absolute_path)), e.instance if isinstance(e.instance, str) else e.message)
            for e in pcm[name].iter_errors(report)}


def test_reports_are_valid_and_paired(validate, pcm):
    result = validate(zero_rates())
    client = {r["report"]["fapiInteractionId"]: r["report"] for r in result.client_reports}
    server = {r["report"]["fapiInteractionId"]: r["report"] for r in result.server_reports}
    assert len(client) == len(result.client_reports) == len(server) == len(result.server_reports) == len(result.results)
    assert result.summary["pcm_reports"] == len(client)

    seen = set()
    for report in [*client.values(), *server.values()]:
        seen |= schema_errors(pcm, report)
    assert seen <= KNOWN_SCHEMA_ERRORS, seen - KNOWN_SCHEMA_ERRORS
    # The walk meets the disagreements, so the list above is not there for nothing.
    assert {e[1] for e in seen} == {"additionalInfo.rejectedBy", "additionalInfo.dropReason"}

    for interaction, report in client.items():
        assert [report[f] for f in PAIRING_FIELDS] == [server[interaction][f] for f in PAIRING_FIELDS]
        assert report["processTimespan"] > 0 and server[interaction]["processTimespan"] > 0
        # The client starts before the server does, and both clocks are this process's.
        assert report["timestamp"] <= server[interaction]["timestamp"]


def test_defective_responses_are_reported_like_the_others(validate, pcm):
    """The statuses the client got, with the defects of the default rates."""
    result = validate(parse_rates(None))
    server = {r["report"]["fapiInteractionId"]: r["report"] for r in result.server_reports}
    assert len(result.client_reports) == len(result.results) == len(server)
    for r in result.client_reports:
        other = server[r["report"]["fapiInteractionId"]]
        assert [r["report"][f] for f in PAIRING_FIELDS] == [other[f] for f in PAIRING_FIELDS]
    status_of = {r["interaction_id"]: r["status"] for r in result.results}
    assert all(status_of[i] == r["statusCode"] for i, r in server.items())
    assert sum(1 for label in result.labels if label["defect_class"]) > 0


def test_endpoints_are_the_required_ones(validate):
    """Both sides report every operation of the requirements, under the same names."""
    required = {(op.split()[0], api["base_path"] + op.split()[1])
                for api in REQUIREMENTS["endpoints"]["apis"].values() for op in api["operations"]}
    result = validate(zero_rates())
    for reports in (result.client_reports, result.server_reports):
        assert {(r["report"]["httpMethod"], r["report"]["endpoint"]) for r in reports} == required
    assert {(m, ENDPOINTS[name]) for m, _, name in ROUTES} == required


def test_additional_info(validate):
    """The portal's required fields, per operation (Consentimento and Contas)."""
    result = validate(zero_rates())
    by_op: dict[tuple, list[dict]] = {}
    for r in result.client_reports:
        by_op.setdefault((r["report"]["httpMethod"], r["report"]["endpoint"].split("/v")[1][2:]), []).append(r["report"])

    def fields(method: str, path: str, ok: bool) -> set[frozenset]:
        return {frozenset(r["additionalInfo"]) for r in by_op[(method, path)] if (r["statusCode"] < 400) == ok}

    assert fields("POST", "consents", True) == {frozenset(
        {"tokenId", "authorisationFlowIntent", "personType", "consentId", "journeyIsLinked", "expirationDateTime"})}
    read = {"tokenId", "consentId", "journeyIsLinked", "expirationDateTime", "status"}
    assert fields("GET", "consents/{consentId}", True) == {
        frozenset(read), frozenset(read | {"rejectedBy", "rejectionReasonCode"})}
    assert fields("POST", "consents/{consentId}/extends", True) == {frozenset({"tokenId", "consentId", "expirationDateTime"})}
    assert fields("POST", "consents/{consentId}/extends", False) == {frozenset()}
    assert fields("GET", "consents/{consentId}/extensions", True) == {frozenset({"tokenId", "consentId", "expirationDateTime"})}
    assert fields("DELETE", "consents/{consentId}", True) == {frozenset({"tokenId", "consentId"})}
    assert fields("GET", "accounts/{accountId}/balances", True) == {frozenset({"tokenId", "consentId"})}
    assert fields("GET", "accounts/{accountId}/balances", False) == {frozenset({"consentId"})}
    for r in result.server_reports:
        create = r["report"]["httpMethod"] == "POST" and r["report"]["endpoint"].endswith("/consents")
        assert r["report"]["additionalInfo"] == ({"dropReason": "NONE"} if create else {})
    # No token, account or customer identifier goes out.
    text = json.dumps([r["report"] for r in result.client_reports + result.server_reports])
    assert "Bearer" not in text and "validator-client-credentials" not in text
    assert not re.search(r"\b\d{11}\b", text)


def test_parties_and_rounding_agree():
    client, server = ClientParties(), ServerParties()
    assert (client.client_org_id, client.server_org_id) == (server.client_org_id, server.server_org_id)
    for seconds in (0.0, 0.0001, 0.001, 0.0011, 1.4999, 1.5):
        assert whole_ms(seconds) == server_whole_ms(seconds) > 0
    assert (whole_ms(0.0), whole_ms(0.0001), whole_ms(1.5)) == (1, 1, 1500)


def test_token_id():
    """72 bits, Base64URL: 12 characters, other for another token or pepper."""
    value = token_id("token", "pepper")
    assert re.fullmatch(r"[A-Za-z0-9_-]{12}", value)
    assert len({value, token_id("other", "pepper"), token_id("token", "other")}) == 3


def test_no_report_without_an_endpoint(specs):
    """An operation outside the specification is not reported; nor is an invalid interaction id."""
    ex = Exchange("GET", "/open-banking/accounts/v2/unknown", "", {}, None, 404, {}, b"", datetime.now(timezone.utc), 0.01)
    assert client_report(spec_for(specs, ex.path), ex, ClientParties()) is None
    ex = Exchange("GET", "/open-banking/accounts/v2/accounts", "", {"x-fapi-interaction-id": "not-a-uuid"}, None, 400, {},
                  b"", datetime.now(timezone.utc), 0.01)
    assert "fapiInteractionId" not in client_report(spec_for(specs, ex.path), ex, ClientParties())
    report = server_report("accounts.list", "GET", 400, "not-a-uuid", datetime.now(timezone.utc), 0.01, ServerParties())
    assert "fapiInteractionId" not in report


def served(handler=None, inner=None):
    """(response, report) of one GET /accounts answered by `handler` (or the ASGI app `inner`)
    behind the middleware."""
    sink = LabelSink(LabelStore(), run_id="r", kind="pcm/server")
    inner = inner or Starlette(routes=[Route("/{path:path}", handler)])
    app = TelemetryMiddleware(inner, sink, ServerParties())

    async def call():
        transport = httpx2.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx2.AsyncClient(transport=transport, base_url="http://x") as client:
            return await client.get("/open-banking/accounts/v2/accounts")

    response = asyncio.run(call())
    assert len(sink.buffer) == 1
    return response, sink.buffer[0]["report"]


def test_server_time_covers_the_answer():
    async def slow(request):
        await asyncio.sleep(0.05)
        return JSONResponse({})

    response, report = served(slow)
    assert response.status_code == report["statusCode"] == 200
    assert 50 <= report["processTimespan"] < 1000


def test_server_time_ends_at_the_last_byte():
    async def chunks():
        yield b"{"
        await asyncio.sleep(0.05)
        yield b"}"

    async def streamed(request):
        return StreamingResponse(chunks(), media_type="application/json")

    _, report = served(streamed)
    assert 50 <= report["processTimespan"] < 1000


def test_server_reports_an_unhandled_error():
    """Nothing was answered yet when the app raises: the 500 comes from outside the middleware."""
    async def broken(scope, receive, send):
        raise RuntimeError("boom")

    response, report = served(inner=broken)
    assert response.status_code == report["statusCode"] == 500


def test_client_time_covers_the_call():
    class SlowClient:
        def request(self, method, url, *, headers, content=None):
            time.sleep(0.05)
            return httpx2.Response(200, json={})

    before = datetime.now(timezone.utc)
    ex, _ = Walker(SlowClient()).call("GET", "/open-banking/accounts/v2/accounts", "urn:x:1")
    assert 0.05 <= ex.seconds < 1
    assert before <= ex.started and (datetime.now(timezone.utc) - ex.started).total_seconds() >= ex.seconds


def test_consent_creation_of_a_company(specs):
    """The dataset has no company yet: the creation's fields that depend on the request."""
    def info(data: dict, status: int = 201) -> dict:
        ex = Exchange("POST", "/open-banking/consents/v3/consents", "", {"authorization": "Bearer t"},
                      json.dumps({"data": data}).encode(), status, {}, b"", datetime.now(timezone.utc), 0.01)
        return client_report(spec_for(specs, ex.path), ex, ClientParties())["additionalInfo"]

    person, company = info({}), info({"businessEntity": {"document": {}}})
    assert (person["personType"], company["personType"]) == ("PF", "PJ")
    assert person["authorisationFlowIntent"] == "HYBRID_FLOW"
    assert (person["journeyIsLinked"], info({"journey": {"isLinked": True}})["journeyIsLinked"]) == ("FALSE", "TRUE")
    assert set(info({}, status=422)) == {"authorisationFlowIntent", "personType", "journeyIsLinked"}


def test_server_uses_the_id_it_generated():
    generated = "d78fc4e5-37ca-4da3-adf2-9b082bf92280"

    async def bad_request(request):
        return JSONResponse({}, 400, {"x-fapi-interaction-id": generated})

    _, report = served(bad_request)
    assert (report["statusCode"], report["fapiInteractionId"]) == (400, generated)
