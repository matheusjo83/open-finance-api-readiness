"""The proxy is transparent when no defect is drawn, every class does what the catalogue says
(contract defects break the specification, business defects keep it and break a domain rule),
decisions are deterministic and the labels cover every exchange."""

from decimal import Decimal

import pytest
from openapi_core.validation.response.exceptions import DataValidationError, MissingRequiredHeader

from defect_proxy.catalogue import ACCOUNTS, BY_ID, CATALOGUE, CONSENTS, Exchange
from defect_proxy.labels import LabelSink, MemoryStore
from defect_proxy.selection import choose, parse_rates

from .conftest import validate, zero_rates

WINDOW = "fromBookingDate=2025-11-17&toBookingDate=2026-11-16"


def walk(seed: dict) -> list[tuple[str, str]]:
    """(url, token) for every seeded resource of every customer, plus the non-happy consents."""
    calls = []
    for customer in seed["customers"]:
        token = customer["consents"][0]
        calls.append((f"{ACCOUNTS}/accounts", token))
        for account in customer["accounts"]:
            base = f"{ACCOUNTS}/accounts/{account}"
            calls += [(base, token), (f"{base}/balances", token), (f"{base}/overdraft-limits", token),
                      (f"{base}/transactions?{WINDOW}", token),
                      (f"{base}/transactions-current?fromBookingDate=2026-11-10&toBookingDate=2026-11-16", token)]
        calls.append((f"{CONSENTS}/consents/{token}", token))
    for consent in seed["customers"][0]["consents"][1:]:
        calls.append((f"{ACCOUNTS}/accounts", consent))
    return calls


def labels(proxy) -> list[dict]:
    client, store = proxy
    client.client.post("/_proxy/flush")
    return store.records()


def test_transparent_without_defects(make_proxy, direct, seed):
    proxy = make_proxy(zero_rates())
    client, _ = proxy
    calls = walk(seed)
    for url, token in calls:
        via_proxy, straight = client.get(url, token), direct.get(url, token)
        assert (via_proxy.status_code, via_proxy.content) == (straight.status_code, straight.content)
        assert via_proxy.headers["x-fapi-interaction-id"] == client.last_headers["x-fapi-interaction-id"]
    records = labels(proxy)
    assert len(records) == len(calls)
    assert all(r["defect_class"] is None and r["run_id"] == "test-run" for r in records)
    assert all(r["interaction_id"] for r in records)


def only(defect_id: str) -> dict[str, float]:
    return {**zero_rates(), defect_id: 1.0}


def first_injected(proxy, calls, specs=None):
    """Calls until the proxy injects; returns (url, response, label, request headers)."""
    client, store = proxy
    for url, token in calls:
        response = client.get(url, token)
        record = labels(proxy)[-1]
        if record["defect_class"]:
            return url, response, record, client.last_headers
    pytest.fail("no applicable request in the walk")


@pytest.mark.parametrize("defect_id", ["missing-required-field", "invalid-format", "missing-interaction-id"])
def test_contract_defects_break_the_spec(defect_id, make_proxy, seed, specs):
    url, response, record, headers = first_injected(make_proxy(only(defect_id)), walk(seed))
    assert record["defect_class"] == defect_id and record["defect_layer"] == "contract" and record["defect_target"]
    expected = MissingRequiredHeader if defect_id == "missing-interaction-id" else DataValidationError
    with pytest.raises(expected):
        validate(specs, "GET", url, headers, response)


def test_undeclared_field_passes_the_spec(make_proxy, seed, specs):
    """The limit of schema validation: nothing in the pinned specs forbids extra fields."""
    url, response, record, headers = first_injected(make_proxy(only("undeclared-field")), walk(seed))
    validate(specs, "GET", url, headers, response)
    assert "internalReference" in response.text


def signed_sum(transactions: list[dict]) -> Decimal:
    return sum((Decimal(t["transactionAmount"]["amount"]) * (1 if t["creditDebitType"] == "CREDITO" else -1)
                for t in transactions if t["completedAuthorisedPaymentType"] == "TRANSACAO_EFETIVADA"), Decimal(0))


def test_inconsistent_balance(make_proxy, direct, seed, specs):
    url, response, record, headers = first_injected(make_proxy(only("inconsistent-balance")), walk(seed))
    validate(specs, "GET", url, headers, response)
    token = next(t for u, t in walk(seed) if u == url)
    history = direct.get(url.replace("/balances", f"/transactions?{WINDOW}&page-size=1000"), token).json()["data"]
    assert Decimal(response.json()["data"]["availableAmount"]["amount"]) != signed_sum(history)


def test_broken_pagination(make_proxy, direct, seed, specs):
    url, response, record, headers = first_injected(make_proxy(only("broken-pagination")), walk(seed))
    validate(specs, "GET", url, headers, response)
    token = next(t for u, t in walk(seed) if u == url)
    clean, broken = direct.get(url, token).json(), response.json()
    variant = record["defect_variant"]
    if variant == "duplicate-item":
        ids = [t.get("transactionId") or t.get("accountId") for t in broken["data"]]
        assert len(set(ids)) < len(ids)
    elif variant == "missing-next":
        assert "next" in clean["links"] and "next" not in broken["links"]
    else:
        assert broken["meta"]["totalRecords"] != clean["meta"]["totalRecords"]


def test_consent_not_enforced(make_proxy, seed, specs):
    expired = next(c for c in seed["customers"][0]["consents"] if c.endswith("-expired"))
    proxy = make_proxy(only("consent-not-enforced"))
    url, response, record, headers = first_injected(proxy, [(f"{ACCOUNTS}/accounts", expired)])
    assert record["upstream_status"] == 401 and record["status"] == 200
    assert response.status_code == 200 and response.json()["data"]
    assert response.headers["x-fapi-interaction-id"] == headers["x-fapi-interaction-id"]
    validate(specs, "GET", url, headers, response)


def test_transaction_outside_window(make_proxy, seed, specs):
    calls = [(u, t) for u, t in walk(seed) if "/transactions?" in u]
    url, response, record, headers = first_injected(make_proxy(only("transaction-outside-window")), calls)
    validate(specs, "GET", url, headers, response)
    dates = [t["transactionDateTime"][:10] for t in response.json()["data"]]
    assert any(not "2025-11-17" <= d <= "2026-11-16" for d in dates)


def test_decisions_are_deterministic(make_proxy, seed):
    def run(defect_seed: int) -> list:
        proxy = make_proxy(parse_rates(None), seed=defect_seed)
        for url, token in walk(seed):
            proxy[0].get(url, token)
        return [(r["path"], r["defect_class"], r["defect_target"]) for r in labels(proxy)]

    first = run(42)
    assert first == run(42)
    assert first != run(43)
    assert any(c for _, c, _ in first)


def test_rates_are_respected():
    """Each class is drawn with probability equal to its rate (one draw over the applicable ones)."""
    rates = {**zero_rates(), "undeclared-field": 0.10, "missing-interaction-id": 0.05}
    hits = {k: 0 for k in rates}
    n = 20000
    for i in range(n):
        ex = Exchange(method="GET", path=f"{ACCOUNTS}/accounts/ACC-{i}", route="accounts.detail", query=[],
                      request_headers={}, status=200, headers={"x-fapi-interaction-id": "x"}, body={"data": {"a": 1}})
        chosen = choose(42, rates, ex)
        if chosen:
            hits[chosen[0].id] += 1
    assert abs(hits["undeclared-field"] / n - 0.10) < 0.01
    assert abs(hits["missing-interaction-id"] / n - 0.05) < 0.01
    assert sum(hits.values()) == hits["undeclared-field"] + hits["missing-interaction-id"]


def test_parse_rates():
    assert parse_rates(None) == {d.id: d.default_rate for d in CATALOGUE}
    assert parse_rates('{"invalid-format": 0.2}')["invalid-format"] == 0.2
    for bad in ['{"nope": 0.1}', '{"invalid-format": 1.5}', '{"invalid-format": 0.9}']:
        with pytest.raises(ValueError):
            parse_rates(bad)
    assert set(BY_ID) == {d.id for d in CATALOGUE} and len(CATALOGUE) == 8


def test_sink_keeps_records_when_a_write_fails():
    class Flaky(MemoryStore):
        fail = True

        def put(self, key, body):
            if self.fail:
                self.fail = False
                raise OSError("store down")
            super().put(key, body)

    store = Flaky()
    sink = LabelSink(store, run_id="r", max_buffer=2)
    assert sink.add({"n": 1}) is False and sink.add({"n": 2}) is True
    with pytest.raises(OSError):
        sink.flush()
    assert sink.flush() == 2 and sink.written == 2
    (key,) = store.objects
    assert key.startswith("labels/run=r/part-00002-") and key.endswith(".jsonl")
    assert [r["n"] for r in store.records()] == [1, 2]


def test_mock_endpoints_pass_through_unlabelled(make_proxy):
    proxy = make_proxy(parse_rates(None))
    assert proxy[0].client.get("/mock/seed").json()["seed"] == 42
    assert labels(proxy) == []
