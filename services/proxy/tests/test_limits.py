"""The limit classes (operational layer): drawn per test case, each variant refuses from the
request it names, the refusals keep the specification (Consents included: 429 is declared there,
423 falls under default), and the server reports carry the caller's origin."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import httpx2
import pytest
from starlette.testclient import TestClient

from defect_proxy.app import create_app
from defect_proxy.catalogue import ACCOUNTS, CONSENTS, ROUTES, Exchange
from defect_proxy.labels import LabelSink, MemoryStore
from defect_proxy.limits import MINIMUMS, ORIGIN_HEADER, LimitDefects, draw
from defect_proxy.selection import parse_rates
from defect_proxy.telemetry import ENDPOINTS, Parties, server_report

from .conftest import NOW, REPO, validate

RATES = parse_rates(json.dumps({"early-traffic-limit": 0.3, "early-operational-limit": 0.3,
                                "limit-not-applicable": 0.3}))


def find(route: str, defect: str, variant: str, k_max: int | None = None) -> str:
    """An origin whose case on `route` draws this class and variant (and a threshold up to k_max)."""
    for i in range(200_000):
        case = draw(42, RATES, f"origin-{i}", route)
        if case and case.defect.id == defect and case.variant == variant and (k_max is None or case.k <= k_max):
            return f"origin-{i}"
    raise AssertionError("no such origin")


def exchange(route: str, status: int = 200, page: int | None = None) -> Exchange:
    path = {"accounts.list": f"{ACCOUNTS}/accounts", "consents.read": f"{CONSENTS}/consents/c"}.get(
        route, f"{ACCOUNTS}/accounts/A1" + ("" if route == "accounts.detail" else "/" + route.split(".")[1]))
    return Exchange(method="GET", path=path, route=route, query=[("page", str(page))] if page else [],
                    request_headers={}, status=status, headers={"content-type": "application/json"},
                    body={"data": {}}, upstream_status=status)


def test_minimums_agree_with_the_requirements():
    req = json.loads((REPO / "specs" / "operational" / "requirements.json").read_text())
    pinned = {op: (v["tpm_min"], v["monthly_calls_min"])
              for api in req["endpoints"]["apis"].values() for op, v in api["operations"].items()}
    methods = {name: method for method, _, name in ROUTES}
    ours = {f"{methods[r]} {ENDPOINTS[r].split('/v2', 1)[-1].split('/v3', 1)[-1]}": v for r, v in MINIMUMS.items()}
    assert ours == pinned


def test_draw_per_case_at_the_rates():
    n = 20_000
    for route, expected in (("accounts.balances", {"early-traffic-limit": 0.3, "early-operational-limit": 0.3}),
                            ("consents.read", {"limit-not-applicable": 0.3})):
        cases = [draw(42, RATES, f"o-{i}", route) for i in range(n)]
        for defect, rate in expected.items():
            assert abs(sum(1 for c in cases if c and c.defect.id == defect) / n - rate) < 0.015
        assert {c.defect.id for c in cases if c} == set(expected)
    # The same case always draws the same; the defaults draw nothing.
    assert draw(42, RATES, "o-1", "accounts.list") == draw(42, RATES, "o-1", "accounts.list")
    assert all(draw(42, parse_rates(None), f"o-{i}", "accounts.list") is None for i in range(1000))


def run(defects: LimitDefects, origin: str, exchanges: list[Exchange], now: datetime = NOW) -> list[int]:
    for ex in exchanges:
        defects.apply(ex, origin, now)
    return [ex.status for ex in exchanges]


@pytest.mark.parametrize("variant", ["random", "off-by-one"])
def test_early_traffic_limit(variant):
    route = "accounts.list"
    origin = find(route, "early-traffic-limit", variant, k_max=None if variant == "off-by-one" else 50)
    case = draw(42, RATES, origin, route)
    assert case.k == 1000 if variant == "off-by-one" else 1 <= case.k < 1000
    defects = LimitDefects(42, RATES)
    codes = run(defects, origin, [exchange(route) for _ in range(case.k + 5)])
    assert codes == [200] * (case.k - 1) + [429] * 6
    # The next minute starts again; another origin is another case.
    assert run(defects, origin, [exchange(route)], NOW + timedelta(minutes=1)) == [200 if case.k > 1 else 429]
    assert run(defects, "someone-else", [exchange(route)]) == [200]


def test_early_operational_limit_random_and_off_by_one():
    route = "accounts.detail"
    for variant in ("random", "off-by-one"):
        origin = find(route, "early-operational-limit", variant)
        k = draw(42, RATES, origin, route).k
        assert k == 8 if variant == "off-by-one" else 1 <= k < 8
        defects = LimitDefects(42, RATES)
        # Errors and later pages neither count nor get refused.
        mixed = [exchange(route, 404), exchange(route, page=2)]
        codes = run(defects, origin, mixed + [exchange(route) for _ in range(k + 2)] + [exchange(route, page=3)])
        assert codes == [404, 200] + [200] * (k - 1) + [423] * 3 + [200]


def test_early_operational_limit_miscounts():
    route = "accounts.transactions"
    origin = find(route, "early-operational-limit", "counts-pagination")
    defects = LimitDefects(42, RATES)
    # One success of a first page and seven later pages: counted with them, eight.
    codes = run(defects, origin, [exchange(route)] + [exchange(route, page=2) for _ in range(7)] + [exchange(route)])
    assert codes == [200] * 8 + [423]

    origin = find(route, "early-operational-limit", "counts-errors")
    defects = LimitDefects(42, RATES)
    codes = run(defects, origin, [exchange(route, 400) for _ in range(7)] + [exchange(route), exchange(route)])
    assert codes == [400] * 7 + [200, 423]


def test_miscount_without_pages_or_errors_changes_nothing():
    route = "accounts.transactions"
    origin = find(route, "early-operational-limit", "counts-pagination")
    defects = LimitDefects(42, RATES)
    # The transmitter's own 423 after eight successes is left alone, so no exchange carries the class.
    exchanges = [exchange(route) for _ in range(8)] + [exchange(route, 423)]
    assert [defects.apply(ex, origin, NOW) for ex in exchanges] == [None] * 9


@pytest.mark.parametrize("variant", ["429", "423"])
def test_limit_not_applicable(variant):
    route = "consents.read"
    origin = find(route, "limit-not-applicable", variant)
    k = draw(42, RATES, origin, route).k
    assert 1 <= k <= 20
    codes = run(LimitDefects(42, RATES), origin, [exchange(route) for _ in range(k + 1)])
    assert codes == [200] * (k - 1) + [int(variant)] * 2


@pytest.fixture
def limits_proxy(transmitter):
    store = MemoryStore()
    clock = {"now": NOW}
    app = create_app(httpx2.ASGITransport(app=transmitter), seed=42, rates=RATES,
                     sink=LabelSink(store, run_id="limits"), flush_seconds=3600, clock=lambda: clock["now"])
    with TestClient(app) as client:
        yield client, store


def call(client, url: str, token: str, origin: str):
    headers = {"x-fapi-interaction-id": str(uuid.uuid4()), "Authorization": f"Bearer {token}", ORIGIN_HEADER: origin}
    return headers, client.get(url, headers=headers)


def test_refusals_through_the_proxy(limits_proxy, seed, specs):
    client, store = limits_proxy
    token = seed["customers"][0]["consents"][0]
    account = seed["customers"][0]["accounts"][0]
    checked = []
    for route, url, defect, variant in [
        ("accounts.balances", f"{ACCOUNTS}/accounts/{account}/balances", "early-traffic-limit", "random"),
        ("accounts.detail", f"{ACCOUNTS}/accounts/{account}", "early-operational-limit", "random"),
        ("consents.read", f"{CONSENTS}/consents/{token}", "limit-not-applicable", "429"),
        ("consents.read", f"{CONSENTS}/consents/{token}", "limit-not-applicable", "423"),
    ]:
        origin = find(route, defect, variant, k_max=20)
        k = draw(42, RATES, origin, route).k
        responses = [call(client, url, token, origin) for _ in range(k)]
        headers, last = responses[-1]
        status = {"early-traffic-limit": 429, "early-operational-limit": 423}.get(defect) or int(variant)
        assert [r.status_code for _, r in responses] == [200] * (k - 1) + [status]
        assert last.headers["x-fapi-interaction-id"] == headers["x-fapi-interaction-id"]
        # Both are valid on Consents too (429 declared, 423 under default): only the operational
        # layer notices them.
        validate(specs, "GET", url, headers, last)
        checked.append((headers["x-fapi-interaction-id"], defect, variant, origin))
    client.post("/_proxy/flush")
    by_id = {r["interaction_id"]: r for r in store.records() if r.get("interaction_id")}
    for interaction, defect, variant, origin in checked:
        label = by_id[interaction]
        assert (label["defect_class"], label["defect_layer"], label["defect_variant"], label["origin"]) == \
            (defect, "operational", variant, origin)
        assert label["upstream_status"] == 200


def test_server_reports_carry_the_origin():
    report = server_report("accounts.list", "GET", 200, None, datetime.now(timezone.utc), 0.01, Parties(), "org-x")
    assert report["clientOrgId"] == "org-x"
    assert server_report("accounts.list", "GET", 200, None, datetime.now(timezone.utc), 0.01,
                         Parties())["clientOrgId"] == Parties().client_org_id
