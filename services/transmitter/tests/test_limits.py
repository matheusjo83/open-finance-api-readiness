"""Traffic and monthly limits at the minimums (IN 615 5.1.1 and 5.2, docs/transmitter.md)."""

from __future__ import annotations

import json
import uuid

import pytest
from fastapi.testclient import TestClient

from transmitter.app import ACCOUNTS, CONSENTS, ORIGIN_HEADER, create_app
from transmitter.limits import MINIMUMS, Limits, alta_tpm, configured

from .conftest import REPO, START


def test_minimums_agree_with_the_requirements():
    req = json.loads((REPO / "specs" / "operational" / "requirements.json").read_text())
    pinned = {op: (v["tpm_min"], v["monthly_calls_min"])
              for api in req["endpoints"]["apis"].values() for op, v in api["operations"].items()}
    assert MINIMUMS == pinned
    bands = req["frequency_classes"]["alta_tpm_by_active_consents"]
    for band in bands:
        assert alta_tpm(band["up_to"]) == band["tpm_min"]
    assert alta_tpm(1) == 2500 and alta_tpm(1_000_001) == 5000
    # Above six million: 2000 more per band of two million (5.1.1 I b: up to eight million, 12000).
    assert alta_tpm(6_000_001) == alta_tpm(8_000_000) == 12000 and alta_tpm(8_000_001) == 14000


def test_overrides_may_raise_limits_only():
    assert configured('{"GET /accounts": {"tpm": 1200}}')["GET /accounts"] == (1200, 8)
    for bad in ('{"GET /accounts": {"monthly": 7}}', '{"POST /consents": {"tpm": 3000}}',
                '{"GET /reserved-balances": {"tpm": 3000}}', '{"GET /accounts": {"tpm": "2000"}}'):
        with pytest.raises(ValueError):
            configured(bad)


def raw(app):
    client = TestClient(app)

    def get(url: str, token: str, origin: str | None = None):
        headers = {"x-fapi-interaction-id": str(uuid.uuid4()), "Authorization": f"Bearer {token}"}
        if origin:
            headers[ORIGIN_HEADER] = origin
        return client.get(url, headers=headers)
    return get


@pytest.fixture
def first(seed) -> tuple[str, str]:
    customer = seed["customers"][0]
    return customer["consents"][0], customer["accounts"][0]


def test_traffic_limit_per_endpoint_origin_and_minute(app, api, clock, first):
    token, _ = first
    get = raw(app)
    origin = str(uuid.uuid4())
    # The monthly limit of the list is 8: raise it out of the way of the minute.
    app.state.limits.limits["GET /accounts"] = (1000, 10_000)
    assert all(get(ACCOUNTS + "/accounts", token, origin).status_code == 200 for _ in range(1000))
    refused = api.get(ACCOUNTS + "/accounts", token=token, headers={ORIGIN_HEADER: origin})
    assert refused.status_code == 429 and refused.json()["errors"][0]["code"] == "LIMITE_TRAFEGO_EXCEDIDO"
    # Another origin, another endpoint, the next minute: each has its own count.
    assert get(ACCOUNTS + "/accounts", token, str(uuid.uuid4())).status_code == 200
    assert get(ACCOUNTS + "/accounts", token).status_code == 200
    assert get(ACCOUNTS + "/accounts/" + first[1], token, origin).status_code == 200
    clock.advance(seconds=60 - START.second)
    assert get(ACCOUNTS + "/accounts", token, origin).status_code == 200


def test_traffic_limit_of_alta():
    # The counter alone (2500 requests through the app take seconds; the path is the same as above).
    limits = Limits(configured(None))
    endpoint = "GET /accounts/{accountId}/balances"
    admitted = [limits.admit_traffic(endpoint, "o", START) for _ in range(2501)]
    assert admitted == [True] * 2500 + [False]


def test_consents_have_no_traffic_limit(app, first):
    token, _ = first
    get = raw(app)
    # Above the lowest minimum (1000), and nothing on Consents counts.
    assert {get(f"{CONSENTS}/consents/{token}", "client", "o").status_code for _ in range(1001)} == {200}
    assert not app.state.limits.minutes


@pytest.mark.parametrize("suffix, limit", [("", 8), ("/balances", 420), ("/overdraft-limits", 420),
                                           ("/transactions-current", 240), ("/transactions", 8)])
def test_monthly_limit_per_account(app, api, first, suffix, limit):
    token, account = first
    get = raw(app)
    url = f"{ACCOUNTS}/accounts/{account}{suffix}"
    assert all(get(url, token, "receiver-a").status_code == 200 for _ in range(limit))
    refused = api.get(url, token=token, headers={ORIGIN_HEADER: "receiver-a"})
    assert refused.status_code == 423 and refused.json()["errors"][0]["code"] == "LIMITE_OPERACIONAL_EXCEDIDO"
    assert get(url, token, "receiver-b").status_code == 200


def test_monthly_limit_counts_successes_of_first_pages_only(app, seed, clock):
    customer = seed["customers"][0]
    token, account = customer["consents"][0], customer["accounts"][0]
    get = raw(app)
    url = f"{ACCOUNTS}/accounts/{account}/transactions"
    for _ in range(7):
        assert get(url, token, "r").status_code == 200
    # Errors and the pages after the first do not count.
    assert get(url + "?creditDebitIndicator=X", token, "r").status_code == 400
    assert get(url + "?page=2&page-size=25", token, "r").status_code == 200
    assert get(f"{ACCOUNTS}/accounts/not-an-account/transactions", token, "r").status_code == 404
    assert get(url, token, "r").status_code == 200  # the eighth
    assert get(url, token, "r").status_code == 423
    # A page after the first is not refused either.
    assert get(url + "?page=2&page-size=25", token, "r").status_code == 200
    # The month turns at midnight in Brasília (03:00 UTC).
    clock.now = clock.now.replace(month=12, day=1, hour=2, minute=59)
    assert get(url, token, "r").status_code == 423
    clock.advance(minutes=1)
    assert get(url, token, "r").status_code == 200


def test_monthly_limit_of_the_list_is_per_consent(app, api, seed):
    customer = seed["customers"][0]
    get = raw(app)
    created = api.post(CONSENTS + "/consents", json={"data": {
        "loggedUser": {"document": {"identification": customer["cpf"], "rel": "CPF"}},
        "permissions": ["ACCOUNTS_READ", "RESOURCES_READ"]}}).json()["data"]["consentId"]
    TestClient(app).post(f"/mock/consents/{created}/authorise")
    for token in (customer["consents"][0], created):
        codes = [get(ACCOUNTS + "/accounts", token, "r").status_code for _ in range(9)]
        assert codes == [200] * 8 + [423]


def test_unauthorised_calls_are_not_counted(app, seed):
    customer = seed["customers"][0]
    expired = next(c for c in customer["consents"] if c.endswith("-expired"))
    get = raw(app)
    assert {get(ACCOUNTS + "/accounts", expired, "r").status_code for _ in range(12)} == {401}


def test_limits_from_the_environment(clock):
    app = create_app(clock=clock, limits='{"GET /accounts": {"monthly": 9}}')
    assert app.state.limits.limits["GET /accounts"] == (1000, 9)
