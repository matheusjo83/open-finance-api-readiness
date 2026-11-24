"""The validator through the proxy: no finding on the clean transmitter, every contract defect
found by the rule meant for it, business defects invisible to it, results joinable with the
labels, and the strict rule on its own."""

import json

import pytest

from contract_validator.results import MemoryStore, PartWriter
from contract_validator.rules import Exchange, check, undeclared_fields
from contract_validator.spec import spec_for
from contract_validator.walk import CLIENT_TOKEN, CLIENT_TOKEN_RENEWED
from defect_proxy.selection import parse_rates

from .conftest import only, zero_rates

CONTRACT = ["missing-required-field", "invalid-format", "missing-interaction-id", "undeclared-field"]
BUSINESS = ["inconsistent-balance", "broken-pagination", "consent-not-enforced", "transaction-outside-window",
            "broken-extension-history", "extension-not-reflected"]
# The rule each contract class must trip (it may trip others too).
EXPECTED_RULE = {
    "missing-required-field": "schema.body",
    "invalid-format": "schema.body",
    "missing-interaction-id": "interaction-id.echo",
    "undeclared-field": "strict.undeclared-field",
}


def test_clean_transmitter_has_no_findings(validate):
    result = validate(zero_rates())
    assert result.results, "the walk made no Open Finance call"
    flagged = [(r["method"], r["path"], r["findings"]) for r in result.results if r["findings"]]
    assert flagged == []
    assert result.summary["verdicts"] == {"pass": len(result.results)}
    # The walk covers every implemented operation, error paths included.
    operations = {r["operation"] for r in result.results}
    assert len(operations) == 11 and None not in operations
    assert {401, 403, 404, 422, 200, 201, 204} <= {r["status"] for r in result.results}


def test_one_result_and_one_exchange_per_label(validate):
    result = validate(zero_rates())
    assert result.summary["run_id"] == "test-run"
    assert len(result.results) == len(result.exchanges) == len(result.labels) == result.summary["exchanges"]
    assert {r["interaction_id"] for r in result.results} == set(result.label_of)
    exchange = next(e for e in result.exchanges if e["operation"] == "accountsGetAccountsAccountIdBalances")
    assert json.loads(exchange["response_body"])["data"]["availableAmount"]
    assert exchange["consent"] and "authorization" not in exchange["request_headers"]


def test_every_consent_is_read_twice_before_use(validate):
    """Before its first use, each consent has been read with both client tokens (decision 33)."""
    result = validate(zero_rates())
    reads: dict[str, set[str]] = {}
    used = 0
    for e in sorted(result.exchanges, key=lambda e: e["seq"]):
        if e["operation"] == "consentsGetConsentsConsentId":
            reads.setdefault(e["path"].rsplit("/", 1)[1], set()).add(e["consent"])
        elif e["operation"].startswith("accountsGet") and e["path"].rsplit("/", 1)[1] != "unknown-account":
            assert reads.get(e["consent"]) == {CLIENT_TOKEN, CLIENT_TOKEN_RENEWED}, e["path"]
            used += 1
    assert used


def test_renewals(validate):
    """Per customer: two renewals (201), the history read with both client tokens, the consent
    read again with the later date, and the two refusals (422)."""
    result = validate(zero_rates())
    by_op: dict[str, list[dict]] = {}
    for e in sorted(result.exchanges, key=lambda e: e["seq"]):
        by_op.setdefault(e["operation"], []).append(e)
    customers = len({e["path"] for e in by_op["consentsGetConsentsConsentIdExtensions"]})
    assert customers == 5
    renewals = by_op["consentsPostConsentsConsentIdExtends"]
    assert sorted(e["status"] for e in renewals) == [201] * 10 + [422] * 10
    histories = by_op["consentsGetConsentsConsentIdExtensions"]
    assert len(histories) == 10 and {e["consent"] for e in histories} == {CLIENT_TOKEN, CLIENT_TOKEN_RENEWED}
    history = json.loads(histories[0]["response_body"])["data"]
    assert [h["expirationDateTime"][:10] for h in history] == ["2027-09-12", "2027-05-15"]
    assert history[0]["previousExpirationDateTime"] == history[1]["expirationDateTime"]
    codes = {json.loads(e["response_body"])["errors"][0]["code"] for e in renewals if e["status"] == 422}
    assert codes == {"ESTADO_CONSENTIMENTO_INVALIDO", "DATA_EXPIRACAO_INVALIDA"}


def test_second_consent_read_is_a_draw_of_its_own(validate):
    """At a rate of one half, some consent gets a defect on one read and not on the other."""
    result = validate({**zero_rates(), "missing-required-field": 0.5})
    outcomes: dict[str, set[bool]] = {}
    for e in result.exchanges:
        if e["operation"] == "consentsGetConsentsConsentId":
            label = result.label_of[e["interaction_id"]]
            outcomes.setdefault(e["path"], set()).add(label["defect_class"] is not None)
    assert any(o == {True, False} for o in outcomes.values())


@pytest.mark.parametrize("defect_id", CONTRACT)
def test_contract_defects_are_found(defect_id, validate):
    result = validate(only(defect_id))
    injected = [(r, label) for r, label in result.joined() if label["defect_class"] == defect_id]
    assert injected, f"{defect_id} never applied in the walk"
    for r, label in injected:
        assert EXPECTED_RULE[defect_id] in r["rules"], (label, r["findings"])
    clean = [r for r, label in result.joined() if label["defect_class"] is None]
    assert all(not r["findings"] for r in clean)


def test_undeclared_field_points_at_the_injected_field(validate):
    result = validate(only("undeclared-field"))
    for r, label in result.joined():
        if label["defect_class"]:
            pointers = [f["pointer"] for f in r["findings"] if f["rule"] == "strict.undeclared-field"]
            assert pointers == [label["defect_target"]]
            assert r["verdict"] == "warn"


def test_missing_interaction_id_needs_the_echo_rule(validate):
    """The schema requires the response header only on Accounts success responses; on Consents
    and on Accounts errors only the echo rule sees it gone."""
    result = validate(only("missing-interaction-id"))
    seen = set()
    for r, label in result.joined():
        if label["defect_class"]:
            accounts_ok = r["api"] == "accounts-2.4.2" and 200 <= r["status"] < 300
            expected = {"interaction-id.echo", "schema.header"} if accounts_ok else {"interaction-id.echo"}
            assert set(r["rules"]) == expected, (r["api"], r["status"], r["rules"])
            seen.add((r["api"], accounts_ok))
    assert seen == {("accounts-2.4.2", True), ("accounts-2.4.2", False), ("consents-3.3.1", False)}


@pytest.mark.parametrize("defect_id", BUSINESS)
def test_business_defects_pass_the_contract(defect_id, validate):
    result = validate(only(defect_id))
    injected = [r for r, label in result.joined() if label["defect_class"] == defect_id]
    assert injected, f"{defect_id} never applied in the walk"
    assert all(not r["findings"] for r, _ in result.joined())


def test_default_rates_contract_layer_is_exact(validate):
    """With the default catalogue: every contract defect found, nothing else flagged."""
    result = validate(parse_rates(None))
    seen = {label["defect_class"] for _, label in result.joined()}
    assert seen & set(CONTRACT)
    for r, label in result.joined():
        assert bool(r["findings"]) == (label["defect_layer"] == "contract"), (label, r["findings"])


def test_strict_rule(specs):
    spec = spec_for(specs, "/open-banking/accounts/v2/accounts")
    schema = {"type": "object", "properties": {
        "a": {"type": "object", "properties": {"b": {"type": "string"}}},
        "free": {"type": "object"},
        "closed": {"type": "object", "properties": {"x": {}}, "additionalProperties": False},
        "map": {"type": "object", "properties": {"x": {}}, "additionalProperties": {"type": "object", "properties": {"v": {}}}},
        "list": {"type": "array", "items": {"allOf": [{"properties": {"p": {}}}, {"properties": {"q": {}}}]}},
    }}
    value = {"a": {"b": "1", "c": 2}, "free": {"any": 1}, "closed": {"y": 1}, "map": {"k": {"v": 1, "w": 2}},
             "list": [{"p": 1, "q": 2}, {"r": 3}], "z/~": 0}
    assert undeclared_fields(spec, schema, value) == ["/a/c", "/map/k/w", "/list/1/r", "/z~1~0"]


def test_operation_and_status_outside_the_spec(specs):
    path = "/open-banking/accounts/v2/accounts/x/unknown"
    ex = Exchange(method="GET", path=path, query="", request_headers={"x-fapi-interaction-id": "i"},
                  request_body=None, status=200, headers={"x-fapi-interaction-id": "i"}, body=b"")
    op, findings = check(spec_for(specs, path), ex)
    assert op is None and [f.rule for f in findings] == ["schema.operation"]


def test_part_writer():
    store = MemoryStore()
    writer = PartWriter(store, "contract/run=r/", "w1", part_size=2)
    for n in range(5):
        writer.add({"n": n})
    writer.flush()
    assert sorted(store.objects) == [f"contract/run=r/part-0000{i}-w1.jsonl" for i in (1, 2, 3)]
    assert [r["n"] for r in store.records()] == list(range(5)) and writer.written == 5
