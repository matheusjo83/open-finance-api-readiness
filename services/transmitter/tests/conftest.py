"""Fixtures: the pinned specifications (openapi-core), a controllable clock and a client that
validates every request and response against the specification of the API it calls."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import yaml
from fastapi.testclient import TestClient
from openapi_core import OpenAPI
from openapi_core.datatypes import RequestParameters
from werkzeug.datastructures import Headers, ImmutableMultiDict

from transmitter.app import ACCOUNTS, CONSENTS, create_app

REPO = Path(__file__).resolve().parents[3]
SPECS = Path(os.environ.get("SPECS_DIR", REPO / "specs" / "upstream"))
SPEC_FILES = {CONSENTS: "consents-3.3.1.yml", ACCOUNTS: "accounts-2.4.2.yml"}
# The specifications' production server; requests are validated as if sent there.
SPEC_HOST = "https://api.banco.com.br"
START = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)


def load_spec(name: str) -> OpenAPI:
    path = SPECS / name
    if not path.exists():
        pytest.exit(f"{path} not found: run `make specs` at the repository root", returncode=2)
    with open(path, encoding="utf-8-sig") as f:  # the Consents file starts with a BOM
        return OpenAPI.from_dict(yaml.safe_load(f))


@pytest.fixture(scope="session")
def specs() -> dict[str, OpenAPI]:
    return {prefix: load_spec(name) for prefix, name in SPEC_FILES.items()}


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs) -> None:
        self.now += timedelta(**kwargs)


class SpecRequest:
    def __init__(self, method: str, url: str, headers: dict, body: bytes | None, content_type: str) -> None:
        parts = urlsplit(url)
        self.host_url, self.path, self.method = SPEC_HOST, parts.path, method.lower()
        query = [tuple(p.split("=", 1)) for p in parts.query.split("&") if p]
        from urllib.parse import unquote_plus
        self.parameters = RequestParameters(
            query=ImmutableMultiDict([(unquote_plus(k), unquote_plus(v)) for k, v in query]),
            header=Headers(headers),
        )
        self.body, self.content_type = body, content_type


class SpecResponse:
    def __init__(self, response) -> None:
        self.status_code = response.status_code
        self.headers = Headers(list(response.headers.items()))
        self.content_type = response.headers.get("content-type", "")
        self.data = response.content or None


class CheckedClient:
    """TestClient wrapper: every call to a specified path is validated against its spec."""

    def __init__(self, client: TestClient, specs: dict[str, OpenAPI]) -> None:
        self.client, self.specs = client, specs

    def spec_for(self, path: str) -> OpenAPI | None:
        return next((spec for prefix, spec in self.specs.items() if path.startswith(prefix)), None)

    def call(self, method: str, url: str, token: str | None = "client-credentials", json=None,
             headers: dict | None = None, check_request: bool = True):
        sent = {"x-fapi-interaction-id": str(uuid.uuid4())}
        if token is not None:
            sent["Authorization"] = f"Bearer {token}"
        if json is not None:
            sent["Content-Type"] = "application/json"
        sent.update(headers or {})
        sent = {k: v for k, v in sent.items() if v is not None}
        response = self.client.request(method, url, headers=sent, json=json)
        spec = self.spec_for(urlsplit(url).path)
        if spec is not None:
            body = response.request.content or None
            request = SpecRequest(method, str(response.request.url), sent, body, sent.get("Content-Type", ""))
            if check_request:
                spec.validate_request(request)
            spec.validate_response(request, SpecResponse(response))
            if "x-fapi-interaction-id" in sent and response.status_code != 400:
                assert response.headers["x-fapi-interaction-id"] == sent["x-fapi-interaction-id"]
        return response

    def get(self, url: str, **kwargs):
        return self.call("GET", url, **kwargs)

    def post(self, url: str, **kwargs):
        return self.call("POST", url, **kwargs)

    def delete(self, url: str, **kwargs):
        return self.call("DELETE", url, **kwargs)


@pytest.fixture
def clock() -> Clock:
    return Clock(START)


@pytest.fixture
def app(clock):
    return create_app(seed=42, customers=5, clock=clock)


@pytest.fixture
def api(app, specs) -> CheckedClient:
    return CheckedClient(TestClient(app), specs)


@pytest.fixture
def seed(api) -> dict:
    return api.client.get("/mock/seed").json()
