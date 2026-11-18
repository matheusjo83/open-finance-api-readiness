"""The detection report over exported runs: the metrics, the gate, the Markdown, and a report
over several runs at once."""

from functools import reduce

import pytest

from metrics import BUSINESS_CLASSES, CONTRACT_CLASSES, aggregates, compute, gate, markdown, per_exchange, wilson


_reports = {}


def report(runs, *names):
    """(aggregates, metrics) over the named runs; aggregates are copied, so tests may edit them."""
    if names not in _reports:
        _reports[names] = _report(runs, *names)
    agg, metrics = _reports[names]
    return {**agg, "by_label": {k: dict(v) for k, v in agg["by_label"].items()}}, metrics


def _report(runs, *names):
    parts = [runs(n) for n in names]
    union = lambda dfs: reduce(lambda a, b: a.unionByName(b), dfs)  # noqa: E731
    rows = per_exchange(union([p.tables["exchanges"] for p in parts]), union([p.tables["contract_results"] for p in parts]),
                        union([p.tables["business_checks"] for p in parts]), union([p.labels for p in parts]))
    agg = aggregates(rows)
    return agg, compute(agg)


def by(rows, key):
    return {r[key]: r for r in rows}


def test_wilson():
    assert wilson(0, 0) == (None, None, None)
    p, low, high = wilson(1, 2)
    assert p == 0.5 and 0.09 < low < 0.1 and 0.9 < high < 0.91
    p, low, high = wilson(10, 10)
    assert p == 1 and high == 1 and 0.72 < low < 0.73


def test_default_run_holds_the_invariants(runs):
    agg, metrics = report(runs, "default")
    assert gate(metrics) == []
    layers = by(metrics["by_layer"], "layer")
    assert layers["contract"]["precision"] == 1 and layers["contract"]["recall_strict"] == 1
    assert layers["business"]["precision"] == 1 and layers["business"]["recall_conditional"] == 1
    classes = by(metrics["by_class"], "defect_class")
    assert sum(classes[c]["positives"] for c in CONTRACT_CLASSES + BUSINESS_CLASSES) == sum(
        v["n"] for k, v in agg["by_label"].items() if k != "clean")
    for c in classes.values():
        assert c["detected"] + c["inconclusive"] + c["missed"] == c["positives"]


def test_rule_precision_and_matrix(runs):
    _, metrics = report(runs, "default")
    rules = by(metrics["by_rule"], "rule")
    assert rules["strict.undeclared-field"]["flagged"] > 0 and rules["strict.undeclared-field"]["precision"] == 1
    # schema.body only ever fires on its two classes.
    assert rules["schema.body"]["precision"] == 1
    cells = {(c["label"], c["rule"]): c["exchanges"] for c in metrics["matrix"]}
    assert not any(label == "clean" for label, _ in cells)
    assert any(label == "missing-interaction-id" and rule == "interaction-id.echo" for label, rule in cells)


def test_gate_catches_a_false_positive(runs):
    agg, _ = report(runs, "default")
    agg["by_label"]["clean"] = {**agg["by_label"]["clean"], "business_flagged": 1}
    problems = gate(compute(agg))
    assert problems and "business precision" in problems[0]


def test_gate_catches_a_miss(runs):
    agg, _ = report(runs, "default")
    row = agg["by_label"]["undeclared-field"]
    agg["by_label"]["undeclared-field"] = {**row, "contract_flagged": row["contract_flagged"] - 1}
    assert any("undeclared-field: 1 missed" in p for p in gate(compute(agg)))


def test_several_runs_add_up(runs):
    one, _ = report(runs, "default")
    both, metrics = report(runs, "default", "clean")
    assert both["runs"] == ["clean", "default"] and both["exchanges"] == one["exchanges"] + runs("clean").exchanges.count()
    assert gate(metrics) == []


@pytest.mark.parametrize("name", ["clean", "default"])
def test_markdown(runs, name):
    agg, metrics = report(runs, name)
    text = markdown(name, agg, metrics, gate(metrics))
    assert text.startswith(f"# Detection report: {name}")
    for section in ("## By layer", "## By defect class", "## By rule", "## Who found what", "## Gate"):
        assert section in text
    assert "All invariants hold" in text or name == "clean"
