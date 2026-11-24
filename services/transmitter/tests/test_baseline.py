"""Clean baseline: every response the transmitter gives, on the happy path and on the
error paths, validates against the pinned specification (checked by the `api` fixture)."""

from datetime import timedelta
from decimal import Decimal

from transmitter.app import ACCOUNTS, CONSENTS
from transmitter.dataset import is_valid_cnpj, is_valid_cpf

from .conftest import START

ACCOUNTS_PERMISSIONS = ["ACCOUNTS_READ", "ACCOUNTS_BALANCES_READ", "ACCOUNTS_TRANSACTIONS_READ",
                        "ACCOUNTS_OVERDRAFT_LIMITS_READ", "RESOURCES_READ"]


def consent_request(cpf: str, days: int = 90, permissions=None) -> dict:
    expiration = (START + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {"data": {
        "loggedUser": {"document": {"identification": cpf, "rel": "CPF"}},
        "permissions": permissions or ACCOUNTS_PERMISSIONS,
        "expirationDateTime": expiration,
    }}


def all_pages(api, url: str, token: str) -> list[dict]:
    rows, next_url = [], url
    while next_url:
        body = api.get(next_url, token=token).json()
        rows += body["data"]
        next_url = body["links"].get("next")
    return rows


def test_every_seeded_resource_validates(api, seed):
    for customer in seed["customers"]:
        token = customer["consents"][0]
        assert token.endswith("-authorised")
        listed = all_pages(api, f"{ACCOUNTS}/accounts?page-size=25", token)
        assert [a["accountId"] for a in listed] == customer["accounts"]
        for account_id in customer["accounts"]:
            base = f"{ACCOUNTS}/accounts/{account_id}"
            assert api.get(base, token=token).status_code == 200
            assert api.get(f"{base}/balances", token=token).status_code == 200
            assert api.get(f"{base}/overdraft-limits", token=token).status_code == 200
            start = (START - timedelta(days=365)).date().isoformat()
            history = all_pages(api, f"{base}/transactions?fromBookingDate={start}&toBookingDate=2027-11-16", token)
            assert history
            current = api.get(f"{base}/transactions-current?fromBookingDate=2026-11-10&toBookingDate=2026-11-16", token=token)
            assert current.status_code == 200


def test_consent_lifecycle(api, seed):
    cpf = seed["customers"][1]["cpf"]
    created = api.post(f"{CONSENTS}/consents", json=consent_request(cpf))
    assert created.status_code == 201
    consent = created.json()["data"]
    assert consent["status"] == "AWAITING_AUTHORISATION"
    consent_id = consent["consentId"]
    url = f"{CONSENTS}/consents/{consent_id}"

    assert api.get(f"{ACCOUNTS}/accounts", token=consent_id).status_code == 401
    assert api.client.post(f"/mock/consents/{consent_id}/authorise").status_code == 200
    assert api.get(url).json()["data"]["status"] == "AUTHORISED"
    assert api.get(f"{ACCOUNTS}/accounts", token=consent_id).status_code == 200

    assert api.delete(url).status_code == 204
    read = api.get(url).json()["data"]
    assert read["status"] == "REJECTED"
    assert read["rejection"] == {"rejectedBy": "USER", "reason": {"code": "CUSTOMER_MANUALLY_REVOKED"}}
    assert api.get(f"{ACCOUNTS}/accounts", token=consent_id).status_code == 401
    again = api.delete(url)
    assert again.status_code == 422
    assert again.json()["errors"][0]["code"] == "CONSENTIMENTO_EM_STATUS_REJEITADO"


def test_awaiting_consent_is_rejected_after_60_minutes(api, seed, clock):
    created = api.post(f"{CONSENTS}/consents", json=consent_request(seed["customers"][0]["cpf"])).json()["data"]
    clock.advance(minutes=61)
    read = api.get(f"{CONSENTS}/consents/{created['consentId']}").json()["data"]
    assert read["status"] == "REJECTED"
    assert read["rejection"]["reason"]["code"] == "CONSENT_EXPIRED"


def test_seeded_non_happy_consents(api, seed):
    """Every customer has the four non-happy consents, after its authorised one."""
    for n, customer in enumerate(seed["customers"], start=1):
        assert customer["consents"] == [f"urn:transmitter:seed-{n}-{s}" for s in
                                        ("authorised", "expired", "revoked", "no-balances", "awaiting")]
        by_suffix = {c.rsplit("-", 1)[-1]: c for c in customer["consents"]}
        account = customer["accounts"][0]

        expired = api.get(f"{CONSENTS}/consents/{by_suffix['expired']}").json()["data"]
        assert expired["status"] == "REJECTED"
        assert expired["rejection"]["reason"]["code"] == "CONSENT_MAX_DATE_REACHED"
        assert api.get(f"{CONSENTS}/consents/{by_suffix['revoked']}").json()["data"]["status"] == "REJECTED"
        assert api.get(f"{CONSENTS}/consents/{by_suffix['awaiting']}").json()["data"]["status"] == "AWAITING_AUTHORISATION"
        for suffix in ("expired", "revoked", "awaiting"):
            assert api.get(f"{ACCOUNTS}/accounts", token=by_suffix[suffix]).status_code == 401
            assert api.get(f"{ACCOUNTS}/accounts/{account}/balances", token=by_suffix[suffix]).status_code == 401
        assert api.get(f"{ACCOUNTS}/accounts", token=by_suffix["balances"]).status_code == 200
        assert api.get(f"{ACCOUNTS}/accounts/{account}/balances", token=by_suffix["balances"]).status_code == 403


def test_error_responses_validate(api, seed):
    token = seed["customers"][0]["consents"][0]
    other = seed["customers"][1]["accounts"][0]
    missing_id = api.get(f"{ACCOUNTS}/accounts", token=token, headers={"x-fapi-interaction-id": None}, check_request=False)
    assert missing_id.status_code == 400
    assert missing_id.headers["x-fapi-interaction-id"]
    assert api.get(f"{ACCOUNTS}/accounts", token=None, check_request=False).status_code == 401
    assert api.get(f"{ACCOUNTS}/accounts/{other}", token=token).status_code == 404
    account = seed["customers"][0]["accounts"][0]
    assert api.get(f"{ACCOUNTS}/accounts/{account}/transactions?toBookingDate=2026-11-16", token=token).status_code == 422
    too_old = api.get(f"{ACCOUNTS}/accounts/{account}/transactions-current?fromBookingDate=2026-11-01&toBookingDate=2026-11-16", token=token)
    assert too_old.status_code == 422
    assert api.get(f"{CONSENTS}/consents/urn:transmitter:unknown").status_code == 404
    bad_date = api.post(f"{CONSENTS}/consents", json=consent_request(seed["customers"][0]["cpf"], days=400))
    assert bad_date.status_code == 422
    assert bad_date.json()["errors"][0]["code"] == "DATA_EXPIRACAO_INVALIDA"
    malformed = api.post(f"{CONSENTS}/consents", json={"data": {"permissions": []}}, check_request=False)
    assert malformed.status_code == 400


def test_pagination_links(api, seed):
    token = seed["customers"][0]["consents"][0]
    account = seed["customers"][0]["accounts"][0]
    url = f"{ACCOUNTS}/accounts/{account}/transactions?fromBookingDate=2025-11-17&toBookingDate=2026-11-16&page-size=10"
    first = api.get(url, token=token).json()
    assert len(first["data"]) == 25  # page-size below 25 is raised to 25
    assert "prev" not in first["links"] and "next" in first["links"]
    second = api.get(first["links"]["next"], token=token).json()
    assert "prev" in second["links"]
    assert {t["transactionId"] for t in first["data"]}.isdisjoint(t["transactionId"] for t in second["data"])


def test_dataset_invariants(api, seed, app):
    """Properties the business-rule layer will check (docs/transmitter.md)."""
    for customer in seed["customers"]:
        token = customer["consents"][0]
        for account_id in customer["accounts"]:
            base = f"{ACCOUNTS}/accounts/{account_id}"
            history = all_pages(api, f"{base}/transactions?fromBookingDate=2025-11-16&toBookingDate=2027-11-16&page-size=1000", token)
            done = [t for t in history if t["completedAuthorisedPaymentType"] == "TRANSACAO_EFETIVADA"]
            signed = sum(Decimal(t["transactionAmount"]["amount"]) * (1 if t["creditDebitType"] == "CREDITO" else -1) for t in done)
            balance = api.get(f"{base}/balances", token=token).json()["data"]
            assert Decimal(balance["availableAmount"]["amount"]) == signed
            assert all(t["transactionDateTime"][:10] > "2026-11-16" for t in history if t["completedAuthorisedPaymentType"] == "LANCAMENTO_FUTURO")
            assert len({t["transactionId"] for t in history}) == len(history)
            current = all_pages(api, f"{base}/transactions-current?fromBookingDate=2026-11-10&toBookingDate=2026-11-16", token)
            assert {t["transactionId"] for t in current} <= {t["transactionId"] for t in history}
            assert history[-1]["type"] == "DEPOSITO"  # newest first: the oldest is the opening deposit
            assert balance["blockedAmount"]["amount"] == balance["automaticallyInvestedAmount"]["amount"] == "0.00"
            amounts = [balance[k] for k in ("availableAmount", "blockedAmount", "automaticallyInvestedAmount")]
            amounts += [t["transactionAmount"] for t in history]
            assert {m["currency"] for m in amounts} == {"BRL"}
            assert all(len(m["amount"].split(".")[1]) == 2 for m in amounts)
            detail = api.get(base, token=token).json()["data"]
            limits = api.get(f"{base}/overdraft-limits", token=token).json()["data"]
            assert bool(limits) == (detail["type"] == "CONTA_DEPOSITO_A_VISTA")
            if limits:
                assert limits["overdraftUsedLimit"]["amount"] == "0.00"
        assert is_valid_cpf(customer["cpf"])
    assert all(is_valid_cpf(t["partieCnpjCpf"]) for t in history if t.get("partiePersonType") == "PESSOA_NATURAL")
    assert all(is_valid_cnpj(t["partieCnpjCpf"]) for t in history if t.get("partiePersonType") == "PESSOA_JURIDICA")
    listed = api.get(f"{ACCOUNTS}/accounts", token=seed["customers"][0]["consents"][0]).json()["data"]
    assert all(is_valid_cnpj(a["companyCnpj"]) for a in listed)


def test_same_seed_same_dataset(specs, clock):
    from fastapi.testclient import TestClient

    from transmitter.app import create_app

    def snapshot(seed: int):
        data = create_app(seed=seed, clock=clock).state.dataset
        return [(a.account_id, a.number, str(a.available_amount), len(a.transactions)) for a in data.accounts.values()]

    assert snapshot(7) == snapshot(7)
    assert snapshot(7) != snapshot(8)
    assert TestClient(create_app(seed=7, clock=clock)).get("/healthz").json() == {"status": "ok"}


def test_harness_rejects_out_of_spec_responses(api, seed, monkeypatch):
    """The validation is real: a lower-case currency (pattern ^[A-Z]{3}$) must be caught."""
    import pytest
    from openapi_core.validation.response.exceptions import DataValidationError

    from transmitter import dataset

    monkeypatch.setattr(dataset, "CURRENCY", "brl")
    customer = seed["customers"][0]
    with pytest.raises(DataValidationError):
        api.get(f"{ACCOUNTS}/accounts/{customer['accounts'][0]}/balances", token=customer["consents"][0])


EXTENSION_HEADERS = {"x-fapi-customer-ip-address": "203.0.113.7", "x-customer-user-agent": "validator/1.0"}


def extend(api, consent_id: str, cpf: str, days: int | None, headers=EXTENSION_HEADERS, token=None, **kwargs):
    data = {"loggedUser": {"document": {"identification": cpf, "rel": "CPF"}}}
    if days is not None:
        data["expirationDateTime"] = (START + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    return api.post(f"{CONSENTS}/consents/{consent_id}/extends", token=token or consent_id, json={"data": data},
                    headers=headers, **kwargs)


def test_consent_extensions(api, seed, clock):
    """Two renewals: each moves the expiration (reflected in the read) and the history lists them
    newest first, each with the expiration it replaced."""
    cpf = seed["customers"][0]["cpf"]
    consent_id = api.post(f"{CONSENTS}/consents", json=consent_request(cpf, days=90)).json()["data"]["consentId"]
    api.client.post(f"/mock/consents/{consent_id}/authorise")
    url = f"{CONSENTS}/consents/{consent_id}"
    assert api.get(f"{url}/extensions").json()["data"] == []

    first = extend(api, consent_id, cpf, 180)
    assert first.status_code == 201
    assert first.json()["data"]["expirationDateTime"] == "2027-05-15T12:00:00Z"
    clock.advance(minutes=1)
    assert extend(api, consent_id, cpf, 300).status_code == 201
    assert api.get(url).json()["data"]["expirationDateTime"] == "2027-09-12T12:00:00Z"

    history = api.get(f"{url}/extensions").json()
    assert [(e["expirationDateTime"], e["previousExpirationDateTime"]) for e in history["data"]] == [
        ("2027-09-12T12:00:00Z", "2027-05-15T12:00:00Z"), ("2027-05-15T12:00:00Z", "2027-02-14T12:00:00Z")]
    assert history["data"][0]["requestDateTime"] > history["data"][1]["requestDateTime"]
    assert history["meta"]["totalRecords"] == 2

    # Indeterminate: no expirationDateTime, in the read and in the history.
    assert extend(api, consent_id, cpf, None).status_code == 201
    assert "expirationDateTime" not in api.get(url).json()["data"]
    assert "expirationDateTime" not in api.get(f"{url}/extensions").json()["data"][0]


def test_consent_extension_refusals(api, seed):
    customer = seed["customers"][0]
    cpf, authorised = customer["cpf"], customer["consents"][0]
    revoked = next(c for c in customer["consents"] if c.endswith("-revoked"))
    state = extend(api, revoked, cpf, 200)
    assert state.status_code == 422 and state.json()["errors"][0]["code"] == "ESTADO_CONSENTIMENTO_INVALIDO"
    earlier = extend(api, authorised, cpf, 30)  # the seeded consent runs for about a year
    assert earlier.status_code == 422 and earlier.json()["errors"][0]["code"] == "DATA_EXPIRACAO_INVALIDA"
    assert extend(api, authorised, cpf, 400).status_code == 422
    assert extend(api, authorised, seed["customers"][1]["cpf"], 200).status_code == 403
    assert extend(api, authorised, cpf, 200, token="client-credentials").status_code == 401
    no_ip = extend(api, authorised, cpf, 200, headers={"x-customer-user-agent": "validator/1.0"}, check_request=False)
    assert no_ip.status_code == 400
    assert api.get(f"{CONSENTS}/consents/{authorised}/extensions").json()["data"] == []
    assert api.get(f"{CONSENTS}/consents/urn:transmitter:unknown/extensions").status_code == 404
