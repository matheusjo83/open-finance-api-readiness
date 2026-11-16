"""Fixtures: the real transmitter in process, the proxy in front of it with an in-memory label
store, and validation against the pinned specifications (openapi-core)."""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import httpx2
import pytest
import yaml
from openapi_core import OpenAPI
from openapi_core.datatypes import RequestParameters
from starlette.testclient import TestClient
from werkzeug.datastructures import Headers, ImmutableMultiDict

from defect_proxy.app import create_app
from defect_proxy.catalogue import ACCOUNTS, CATALOGUE, CONSENTS
from defect_proxy.labels import LabelSink, MemoryStore
from transmitter.app import create_app as create_transmitter

REPO = Path(__file__).resolve().parents[3]
SPECS = Path(os.environ.get("SPECS_DIR", REPO / "specs" / "upstream"))
SPEC_FILES = {CONSENTS: "consents-3.3.1.yml", ACCOUNTS: "accounts-2.4.2.yml"}
SPEC_HOST = "https://api.banco.com.br"
NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def specs() -> dict[str, OpenAPI]:
    out = {}
    for prefix, name in SPEC_FILES.items():
        path = SPECS / name
        if not path.exists():
            pytest.exit(f"{path} not found: run `make specs` at the repository root", returncode=2)
        with open(path, encoding="utf-8-sig") as f:
            out[prefix] = OpenAPI.from_dict(yaml.safe_load(f))
    return out


class SpecRequest:
    def __init__(self, method: str, url: str, headers: dict) -> None:
        parts = urlsplit(url)
        self.host_url, self.path, self.method = SPEC_HOST, parts.path, method.lower()
        self.parameters = RequestParameters(query=ImmutableMultiDict(parse_qsl(parts.query)), header=Headers(headers))
        self.body, self.content_type = None, ""


class SpecResponse:
    def __init__(self, response) -> None:
        self.status_code = response.status_code
        self.headers = Headers(list(response.headers.items()))
        self.content_type = response.headers.get("content-type", "")
        self.data = response.content or None


def validate(specs: dict[str, OpenAPI], method: str, url: str, headers: dict, response) -> None:
    """Raises if the response breaks the specification of the API it belongs to."""
    path = urlsplit(url).path
    spec = next(s for prefix, s in specs.items() if path.startswith(prefix))
    spec.validate_response(SpecRequest(method, url, headers), SpecResponse(response))


class Client:
    """Calls with the headers a receiving institution sends; remembers them for validation."""

    def __init__(self, client: TestClient) -> None:
        self.client = client
        self.last_headers: dict = {}

    def get(self, url: str, token: str | None = None):
        headers = {"x-fapi-interaction-id": str(uuid.uuid4())}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        self.last_headers = headers
        return self.client.get(url, headers=headers)


@pytest.fixture
def transmitter():
    return create_transmitter(seed=42, customers=5, clock=lambda: NOW)


@pytest.fixture
def direct(transmitter) -> Client:
    return Client(TestClient(transmitter))


@pytest.fixture
def seed(direct) -> dict:
    return direct.client.get("/mock/seed").json()


def zero_rates() -> dict[str, float]:
    return {d.id: 0.0 for d in CATALOGUE}


@pytest.fixture
def make_proxy(transmitter):
    """make_proxy(rates, seed=42) -> (Client, MemoryStore); flush labels with client.client.post('/_proxy/flush')."""
    opened = []

    def make(rates: dict[str, float], seed: int = 42):
        store = MemoryStore()
        app = create_app(httpx2.ASGITransport(app=transmitter), seed=seed, rates=rates,
                         sink=LabelSink(store, run_id="test-run"), flush_seconds=3600)
        client = TestClient(app)
        client.__enter__()
        opened.append(client)
        return Client(client), store

    yield make
    for client in opened:
        client.__exit__(None, None, None)
