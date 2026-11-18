"""Detection metrics (docs/report.md): what each layer found, against the proxy's labels.

The unit is the exchange (interaction_id). A label marks an exchange positive for its defect
class; each class belongs to one layer and is measured against that layer:

- contract layer: flagged when the contract verdict is fail or warn;
- business layer: flagged with at least one finding; inconclusive when none, but a rule could
  not decide.

Per class: detected, inconclusive, missed, recall (strict: inconclusive counts as missed;
conditional: over the conclusive positives). Per rule: precision against the classes the rule
targets. Per layer: precision against any defect of that layer. Proportions come with a 95 %
Wilson interval. The aggregates are computed in Spark; the metrics, the gate and the Markdown in
the driver (a handful of rows).
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

CONTRACT_CLASSES = ["missing-required-field", "invalid-format", "missing-interaction-id", "undeclared-field"]
BUSINESS_CLASSES = ["inconsistent-balance", "broken-pagination", "consent-not-enforced", "transaction-outside-window"]
LAYER_OF = {**{c: "contract" for c in CONTRACT_CLASSES}, **{c: "business" for c in BUSINESS_CLASSES}}
# The classes each rule is meant to find (precision per rule). The schema rules for status,
# content type and operation aim at no class in particular: any contract defect counts.
TARGETS = {
    "schema.body": ["missing-required-field", "invalid-format"],
    "schema.header": ["missing-interaction-id"],
    "schema.status": CONTRACT_CLASSES, "schema.content-type": CONTRACT_CLASSES, "schema.operation": CONTRACT_CLASSES,
    "interaction-id.echo": ["missing-interaction-id"],
    "strict.undeclared-field": ["undeclared-field"],
    "balance.matches-transactions": ["inconsistent-balance"],
    "pagination.duplicate-item": ["broken-pagination"],
    "pagination.missing-next": ["broken-pagination"],
    "pagination.total-records": ["broken-pagination"],
    "consent.enforced": ["consent-not-enforced"],
    "transactions.within-window": ["transaction-outside-window"],
}
CLEAN = "clean"
Z = 1.959964  # 95 %


def wilson(successes: int, total: int) -> tuple[float | None, float | None, float | None]:
    """(estimate, low, high) of a proportion; all None without observations."""
    if total == 0:
        return None, None, None
    p = successes / total
    centre = p + Z * Z / (2 * total)
    margin = Z * math.sqrt(p * (1 - p) / total + Z * Z / (4 * total * total))
    scale = 1 + Z * Z / total
    low = 0.0 if successes == 0 else max(0.0, (centre - margin) / scale)
    high = 1.0 if successes == total else min(1.0, (centre + margin) / scale)
    return p, low, high


def per_exchange(exchanges: DataFrame, contract: DataFrame, checks: DataFrame, labels: DataFrame) -> DataFrame:
    """One row per exchange: its label and what each layer said."""
    business = checks.groupBy("interaction_id").agg(
        F.max(F.col("outcome") == "finding").alias("business_flagged"),
        F.max(F.col("outcome") == "inconclusive").alias("business_inconclusive"),
        F.array_sort(F.array_distinct(F.collect_list(F.when(F.col("outcome") == "finding", F.col("rule")))))
        .alias("business_rules"))
    verdicts = contract.select("interaction_id", F.col("verdict").alias("contract_verdict"),
                               F.col("rules").alias("contract_rules"))
    return (exchanges.select("run_id", "interaction_id", "operation", "status")
            .join(labels.select("interaction_id", "defect_class", "defect_layer"), "interaction_id", "left")
            .join(verdicts, "interaction_id", "left").join(business, "interaction_id", "left")
            .select("run_id", "interaction_id", "operation", "status",
                    F.coalesce("defect_class", F.lit(CLEAN)).alias("label"),
                    F.col("defect_layer"),
                    F.col("contract_verdict").isin("fail", "warn").alias("contract_flagged"),
                    F.coalesce("business_flagged", F.lit(False)).alias("business_flagged"),
                    (F.coalesce("business_inconclusive", F.lit(False)) & ~F.coalesce("business_flagged", F.lit(False)))
                    .alias("business_inconclusive"),
                    F.coalesce("contract_rules", F.array().cast("array<string>")).alias("contract_rules"),
                    F.coalesce("business_rules", F.array().cast("array<string>")).alias("business_rules")))


def aggregates(rows: DataFrame) -> dict[str, Any]:
    """The counts the metrics need, collected to the driver."""
    by_label = {r["label"]: r.asDict() for r in rows.groupBy("label").agg(
        F.count("*").alias("n"),
        F.sum(F.col("contract_flagged").cast("int")).alias("contract_flagged"),
        F.sum(F.col("business_flagged").cast("int")).alias("business_flagged"),
        F.sum(F.col("business_inconclusive").cast("int")).alias("business_inconclusive")).collect()}
    fired = (rows.select("label", F.explode(F.concat("contract_rules", "business_rules")).alias("rule"))
             .groupBy("label", "rule").count().collect())
    matrix: dict[str, dict[str, int]] = {}
    for r in fired:
        matrix.setdefault(r["label"], {})[r["rule"]] = r["count"]
    return {"by_label": by_label, "matrix": matrix, "exchanges": rows.count(),
            "runs": sorted(r["run_id"] for r in rows.select("run_id").distinct().collect())}


def compute(agg: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    by_label, matrix = agg["by_label"], agg["matrix"]
    classes = []
    for cls in CONTRACT_CLASSES + BUSINESS_CLASSES:
        layer = LAYER_OF[cls]
        row = by_label.get(cls, {})
        n = row.get("n", 0) or 0
        detected = (row.get(f"{layer}_flagged") or 0)
        inconclusive = 0 if layer == "contract" else min(row.get("business_inconclusive") or 0, n - detected)
        strict, s_lo, s_hi = wilson(detected, n)
        cond, c_lo, c_hi = wilson(detected, n - inconclusive)
        classes.append({"defect_class": cls, "layer": layer, "positives": n, "detected": detected,
                        "inconclusive": inconclusive, "missed": n - detected - inconclusive,
                        "recall_strict": strict, "recall_strict_low": s_lo, "recall_strict_high": s_hi,
                        "recall_conditional": cond, "recall_conditional_low": c_lo, "recall_conditional_high": c_hi})

    rules = []
    for rule, targets in TARGETS.items():
        flagged = sum(counts.get(rule, 0) for counts in matrix.values())
        true = sum(matrix.get(c, {}).get(rule, 0) for c in targets)
        p, lo, hi = wilson(true, flagged)
        rules.append({"rule": rule, "layer": "contract" if rule.startswith(("schema.", "interaction-id.", "strict."))
                      else "business", "targets": ",".join(targets), "flagged": flagged, "true_positives": true,
                      "precision": p, "precision_low": lo, "precision_high": hi})

    layers = []
    for layer in ("contract", "business"):
        flagged = sum((row.get(f"{layer}_flagged") or 0) for row in by_label.values())
        true = sum((row.get(f"{layer}_flagged") or 0) for label, row in by_label.items() if LAYER_OF.get(label) == layer)
        own = [c for c in classes if c["layer"] == layer]
        positives, detected = sum(c["positives"] for c in own), sum(c["detected"] for c in own)
        inconclusive = sum(c["inconclusive"] for c in own)
        p, p_lo, p_hi = wilson(true, flagged)
        r, r_lo, r_hi = wilson(detected, positives)
        rc, rc_lo, rc_hi = wilson(detected, positives - inconclusive)
        layers.append({"layer": layer, "flagged": flagged, "true_positives": true, "precision": p,
                       "precision_low": p_lo, "precision_high": p_hi, "positives": positives, "detected": detected,
                       "inconclusive": inconclusive, "recall_strict": r, "recall_strict_low": r_lo,
                       "recall_strict_high": r_hi, "recall_conditional": rc, "recall_conditional_low": rc_lo,
                       "recall_conditional_high": rc_hi})

    cells = [{"label": label, "rule": rule, "exchanges": n}
             for label, counts in sorted(matrix.items()) for rule, n in sorted(counts.items())]
    return {"by_class": classes, "by_rule": rules, "by_layer": layers, "matrix": cells}


def gate(metrics: dict[str, list[dict[str, Any]]]) -> list[str]:
    """The invariants the catalogue guarantees; a non-empty list fails the job (--gate)."""
    problems = []
    for layer in metrics["by_layer"]:
        if layer["flagged"] and layer["precision"] != 1:
            problems.append(f"{layer['layer']} precision {layer['precision']:.3f} "
                            f"({layer['flagged'] - layer['true_positives']} false positives)")
    for c in metrics["by_class"]:
        if c["recall_conditional"] is not None and c["recall_conditional"] != 1:
            problems.append(f"{c['defect_class']}: {c['missed']} missed of {c['positives'] - c['inconclusive']} conclusive")
        if c["layer"] == "contract" and c["inconclusive"]:
            problems.append(f"{c['defect_class']}: contract checks cannot be inconclusive")
    if not any(c["detected"] for c in metrics["by_class"]):
        problems.append("no defect detected at all")
    return problems


def _pct(value: float | None, low: float | None = None, high: float | None = None) -> str:
    if value is None:
        return "—"
    text = f"{value:.0%}" if value in (0, 1) else f"{value:.1%}"
    return text if low is None else f"{text} [{low:.0%}–{high:.0%}]"


def markdown(name: str, agg: dict[str, Any], metrics: dict[str, list[dict[str, Any]]], problems: Iterable[str]) -> str:
    lines = [f"# Detection report: {name}", "",
             f"Runs: {', '.join(f'`{r}`' for r in agg['runs'])}. Exchanges: {agg['exchanges']}. "
             "Intervals: 95 % Wilson.", "", "## By layer", "",
             "| Layer | Precision | Recall (strict) | Recall (conditional) | Detected | Inconclusive | Positives |",
             "|---|---|---|---|---|---|---|"]
    for r in metrics["by_layer"]:
        lines.append(f"| {r['layer']} | {_pct(r['precision'], r['precision_low'], r['precision_high'])} "
                     f"| {_pct(r['recall_strict'], r['recall_strict_low'], r['recall_strict_high'])} "
                     f"| {_pct(r['recall_conditional'], r['recall_conditional_low'], r['recall_conditional_high'])} "
                     f"| {r['detected']} | {r['inconclusive']} | {r['positives']} |")
    lines += ["", "## By defect class", "",
              "| Class | Layer | Positives | Detected | Inconclusive | Missed | Recall (strict) | Recall (conditional) |",
              "|---|---|---|---|---|---|---|---|"]
    for c in metrics["by_class"]:
        lines.append(f"| `{c['defect_class']}` | {c['layer']} | {c['positives']} | {c['detected']} | {c['inconclusive']} "
                     f"| {c['missed']} | {_pct(c['recall_strict'], c['recall_strict_low'], c['recall_strict_high'])} "
                     f"| {_pct(c['recall_conditional'], c['recall_conditional_low'], c['recall_conditional_high'])} |")
    lines += ["", "## By rule", "", "| Rule | Layer | Flagged | Hits on its targets | Precision |", "|---|---|---|---|---|"]
    for r in metrics["by_rule"]:
        if r["flagged"]:
            lines.append(f"| `{r['rule']}` | {r['layer']} | {r['flagged']} | {r['true_positives']} "
                         f"| {_pct(r['precision'], r['precision_low'], r['precision_high'])} |")
    rules = sorted({cell["rule"] for cell in metrics["matrix"]})
    lines += ["", "## Who found what", "", "Exchanges per label (rows) and rule that fired (columns).", "",
              "| Label | " + " | ".join(f"`{r}`" for r in rules) + " |", "|---|" + "---|" * len(rules)]
    counts = {(c["label"], c["rule"]): c["exchanges"] for c in metrics["matrix"]}
    for label in [CLEAN] + CONTRACT_CLASSES + BUSINESS_CLASSES:
        if label in agg["by_label"]:
            lines.append(f"| `{label}` | " + " | ".join(str(counts.get((label, r), "")) for r in rules) + " |")
    problems = list(problems)
    lines += ["", "## Gate", ""] + ([f"- {p}" for p in problems] if problems else
                                    ["All invariants hold: precision 1 in both layers, conditional recall 1 in every "
                                     "class, no inconclusive contract check."])
    return "\n".join(lines) + "\n"
