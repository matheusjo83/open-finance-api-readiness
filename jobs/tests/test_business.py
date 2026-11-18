"""The business rules over exported runs: nothing found on the clean transmitter, every business
defect found by the rule meant for it, and the contract layer's failures left out. The
comparison with the labels over a whole run is the report's (test_report.py)."""

import pytest

EXPECTED_RULE = {
    "inconsistent-balance": "balance.matches-transactions",
    "broken-pagination": "pagination.",
    "consent-not-enforced": "consent.enforced",
    "transaction-outside-window": "transactions.within-window",
}
RULES = {"balance.matches-transactions", "pagination.duplicate-item", "pagination.missing-next",
         "pagination.total-records", "consent.enforced", "transactions.within-window"}


def findings(run):
    return [c for c in run.checks if c["outcome"] == "finding"]


def test_clean_run_has_no_findings(runs):
    run = runs("clean")
    assert findings(run) == []
    assert {c["rule"] for c in run.checks} == RULES
    assert [c for c in run.checks if c["outcome"] == "inconclusive"] == []
    # Every rule was evaluated where it applies: balances, pages, accounts calls.
    by_rule = {}
    for c in run.checks:
        by_rule[c["rule"]] = by_rule.get(c["rule"], 0) + 1
    assert by_rule["balance.matches-transactions"] == run.tables["balances"].count() > 0
    assert by_rule["pagination.missing-next"] == run.tables["listing_pages"].count() > 0
    assert by_rule["consent.enforced"] > 0


def test_silver_tables(runs):
    run = runs("clean")
    assert run.tables["consent_reads"].where("consent_status != 'AUTHORISED'").count() >= 3
    items = run.tables["listing_items"]
    assert items.where("amount IS NULL AND operation != 'accountsGetAccounts'").count() == 0
    pages = run.tables["listing_pages"]
    assert pages.where("page > 1").count() > 0, "no multi-page listing in the walk"


@pytest.mark.parametrize("defect_id", list(EXPECTED_RULE))
def test_business_defects_are_found(defect_id, runs):
    run = runs(defect_id)
    injected = {i for i, label in run.label_of.items() if label["defect_class"] == defect_id}
    assert injected, f"{defect_id} never applied in the walk"
    flagged = {}
    for c in findings(run):
        flagged.setdefault(c["interaction_id"], set()).add(c["rule"])
    missed = injected - set(flagged)
    assert not missed, [run.label_of[i] for i in missed]
    assert set(flagged) <= injected, [i for i in flagged if i not in injected]
    for i in injected:
        assert any(rule.startswith(EXPECTED_RULE[defect_id]) for rule in flagged[i]), (run.label_of[i], flagged[i])


def test_pagination_variants_each_have_their_rule(runs):
    run = runs("broken-pagination")
    rule_of = {"duplicate-item": "pagination.duplicate-item", "missing-next": "pagination.missing-next",
               "wrong-total": "pagination.total-records"}
    by_interaction = {}
    for c in findings(run):
        by_interaction.setdefault(c["interaction_id"], set()).add(c["rule"])
    variants = set()
    for i, label in run.label_of.items():
        if label["defect_class"] == "broken-pagination":
            variants.add(label["defect_variant"])
            assert rule_of[label["defect_variant"]] in by_interaction.get(i, set()), label
    assert variants == set(rule_of)


def test_contract_failures_are_left_out(runs):
    run = runs("default")
    failed = {r["interaction_id"] for r in run.contract.where("verdict = 'fail'").collect()}
    assert failed
    assert not failed & {c["interaction_id"] for c in run.checks}
