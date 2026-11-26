"""Operational detection (docs/operational-report.md, item 18): what each verdict of the
operational job flags against the episodes a history built.

The unit is the verdict's own: an endpoint on a day or in a month, an endpoint over its 90 days,
an API or the institution on a day or in a month. Each unit carries the causes the history put
in it (the classes and edges of its days) and whether the verdict flagged it.

Per verdict:

- targets: the classes it exists to find; recall is measured on the units that have one;
- allowed: the causes that may flag it as well, as the requirements read (a 529 is an error in
  availability, for instance); precision counts a flag right when its unit has an allowed cause;
- edges: units built exactly at a threshold, with the verdict the requirements give there; they
  must come out as expected.

The verdicts come from the job's tables, the causes from the labels: nothing here recomputes a
metric. The aggregates are small and computed in the driver.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any, Iterable

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

Z = 1.959964  # 95 %
LONG_DAYS = 90  # the window of the long availability (requirements: availability.long_window_days)


def _any(*names: str) -> set[str]:
    return set(names)


# verdict -> (scope, flag column (True = flagged), targets, allowed, edges flagged / not flagged).
# A cause is "class/variant" or "edge:<name>"; a target or allowed entry is a class, or class/variant.
VERDICTS: dict[str, dict[str, Any]] = {
    "rt_day": {"scope": "endpoint-day", "targets": _any("slow-day"), "allowed": _any("slow-day"),
               "edges": {"edge:p95-at-sla": False}},
    "availability_day": {"scope": "endpoint-day", "targets": _any("outage/daily"),
                         "allowed": _any("outage", "overload", "edge:overload-at-0.5"),
                         "edges": {"edge:minute-at-95": False, "edge:day-at-95": False}},
    "long_availability": {"scope": "endpoint", "targets": _any("outage-chronic"),
                          "allowed": _any("outage-chronic", "outage", "overload", "edge:overload-at-0.5"), "edges": {}},
    "overload_day": {"scope": "institution-day", "targets": _any("overload"), "allowed": _any("overload"),
                     "edges": {"edge:overload-at-0.5": True}},
    "reporting_8h_day": {"scope": "api-day", "targets": _any("unpaired/late-client", "unpaired/late-server",
                                                             "unpaired/missing-many-client", "unpaired/missing-many-server"),
                         "allowed": _any("unpaired"), "edges": {}},
    "reporting_7d_day": {"scope": "api-day", "targets": _any("unpaired/missing-some-client", "unpaired/missing-some-server",
                                                             "unpaired/missing-many-client", "unpaired/missing-many-server",
                                                             "inconsistent-pair"),
                         "allowed": _any("unpaired", "inconsistent-pair"), "edges": {}},
    "rt_month": {"scope": "endpoint-month", "targets": _any("slow-day/severe"), "allowed": _any("slow-day"),
                 "edges": {}},
    "availability_month": {"scope": "endpoint-month", "targets": _any("outage/daily"),
                           "allowed": _any("outage", "outage-chronic", "overload", "edge:overload-at-0.5"), "edges": {}},
    "overload_month": {"scope": "institution-month", "targets": _any("overload/over-5"), "allowed": _any("overload"),
                       "edges": {}},
    "reporting_month": {"scope": "api-month", "targets": _any("unpaired/missing-some-client",
                                                              "unpaired/missing-some-server", "unpaired/missing-many-client",
                                                              "unpaired/missing-many-server", "inconsistent-pair"),
                        "allowed": _any("unpaired", "inconsistent-pair"), "edges": {}},
}


def matches(cause: str, entries: set[str]) -> bool:
    return cause in entries or cause.split("/")[0] in entries


def _cause(df: DataFrame):
    """class/variant, edge:<name> or null. A label file leaves out a column that is always null
    (the API scope has no edges)."""
    edge = F.col("edge") if "edge" in df.columns else F.lit(None).cast("string")
    return (F.when(F.col("class").isNotNull(), F.concat_ws("/", "class", "variant"))
            .when(edge.isNotNull(), F.concat(F.lit("edge:"), edge)))


def units(tables: dict[str, DataFrame], labels: dict[str, DataFrame], run_id: str) -> DataFrame:
    """One row per (verdict, unit): run_id, verdict, unit, causes (the unit's own: they make it a
    positive), context (more causes that may explain a flag: the 90-day window of a long
    availability), flagged."""
    day = labels["endpoint-day"].select(F.col("endpoint"), F.col("method").alias("httpMethod"), "day", "api",
                                        "chronic", _cause(labels["endpoint-day"]).alias("cause"))
    # The endpoint's chronic outage is a cause of every day it chose.
    day = day.withColumn("cause", F.when(F.col("chronic"), F.lit("outage-chronic")).otherwise(F.col("cause")))
    by_day = day.groupBy("endpoint", "httpMethod", "day").agg(F.collect_set("cause").alias("causes"))
    month = F.substring("day", 1, 7)

    ours = tables["endpoint_day"].withColumn("day", F.date_format("day", "yyyy-MM-dd"))
    out = []

    empty = F.array().cast("array<string>")

    def add(verdict: str, df: DataFrame, unit, flag) -> None:
        context = F.coalesce("context", empty) if "context" in df.columns else empty
        out.append(df.select(F.lit(run_id).alias("run_id"), F.lit(verdict).alias("verdict"), unit.alias("unit"),
                             F.coalesce("causes", empty).alias("causes"), context.alias("context"),
                             flag.alias("flagged")))

    endpoint_day = ours.join(by_day, ["endpoint", "httpMethod", "day"], "left")
    key = F.concat_ws(" ", "httpMethod", "endpoint", "day")
    add("rt_day", endpoint_day.where(F.col("rt_ok").isNotNull()), key, ~F.col("rt_ok"))
    add("availability_day", endpoint_day.where(F.col("availability_ok").isNotNull()), key, ~F.col("availability_ok"))

    def window_causes(ends: DataFrame) -> DataFrame:
        """ends (endpoint, httpMethod, end, ...) with the causes of the 90 days up to `end`: what
        the long availability of that day averages over."""
        days = by_day.select("endpoint", "httpMethod", F.col("day").alias("cause_day"), F.col("causes").alias("c"))
        joined = ends.join(days, ["endpoint", "httpMethod"], "left").where(
            F.col("cause_day").isNull() | (F.datediff(F.to_date("end"), F.to_date("cause_day")).between(0, LONG_DAYS - 1)))
        keys = [c for c in ends.columns]
        return joined.groupBy(*keys).agg(F.array_distinct(F.flatten(F.collect_list("c"))).alias("context"))

    # The 90-day availability at the last day, with the causes of its window.
    last = ours.groupBy("endpoint", "httpMethod").agg(F.max("day").alias("end"))
    # Its window is the unit itself: the window's causes make it a positive.
    long = (ours.join(last.withColumnRenamed("end", "day"), ["endpoint", "httpMethod", "day"])
            .join(window_causes(last).withColumnRenamed("context", "causes"), ["endpoint", "httpMethod"], "left"))
    add("long_availability", long, F.concat_ws(" ", "httpMethod", "endpoint"), ~F.col("long_ok"))

    month_causes = (by_day.withColumn("month", month).groupBy("endpoint", "httpMethod", "month")
                    .agg(F.array_distinct(F.flatten(F.collect_list("causes"))).alias("causes")))
    endpoint_month = tables["endpoint_month"].join(month_causes, ["endpoint", "httpMethod", "month"], "left")
    mkey = F.concat_ws(" ", "httpMethod", "endpoint", "month")
    add("rt_month", endpoint_month.where(F.col("rt_month_ok").isNotNull()), mkey, ~F.col("rt_month_ok"))
    # The month's availability also takes the long availability of its last day: the causes of
    # that day's 90-day window, earlier months included.
    month_end = (ours.withColumn("month", month).groupBy("endpoint", "httpMethod", "month").agg(F.max("day").alias("end")))
    availability_month = (endpoint_month.where(F.col("availability_month_ok").isNotNull())
                          .join(window_causes(month_end), ["endpoint", "httpMethod", "month"], "left"))
    add("availability_month", availability_month, mkey, ~F.col("availability_month_ok"))

    inst_causes = (labels["institution-day"].select("day", _cause(labels["institution-day"]).alias("cause"))
                   .groupBy("day").agg(F.collect_set("cause").alias("causes")))
    inst = tables["institution_day"].withColumn("day", F.date_format("day", "yyyy-MM-dd")).join(inst_causes, "day", "left")
    add("overload_day", inst.where(F.col("overload_ok").isNotNull()), F.col("day"), ~F.col("overload_ok"))
    inst_month_causes = (inst_causes.withColumn("month", month).groupBy("month")
                         .agg(F.array_distinct(F.flatten(F.collect_list("causes"))).alias("causes")))
    add("overload_month", tables["institution_month"].join(inst_month_causes, "month", "left"), F.col("month"),
        ~F.col("overload_month_ok"))

    api_causes = (labels["api-day"].select("api", "day", _cause(labels["api-day"]).alias("cause"))
                  .groupBy("api", "day").agg(F.collect_set("cause").alias("causes")))
    api = tables["api_day"].withColumn("day", F.date_format("day", "yyyy-MM-dd")).join(api_causes, ["api", "day"], "left")
    akey = F.concat_ws(" ", "api", "day")
    add("reporting_8h_day", api, akey, ~F.col("reporting_8h_ok"))
    add("reporting_7d_day", api, akey, ~F.col("reporting_7d_ok"))
    api_month_causes = (api_causes.withColumn("month", month).groupBy("api", "month")
                        .agg(F.array_distinct(F.flatten(F.collect_list("causes"))).alias("causes")))
    add("reporting_month", tables["api_month"].join(api_month_causes, ["api", "month"], "left"),
        F.concat_ws(" ", "api", "month"), ~F.col("reporting_month_ok"))

    result = out[0]
    for df in out[1:]:
        result = result.unionByName(df)
    return result


def wilson(successes: int, total: int) -> tuple[float | None, float | None, float | None]:
    if total == 0:
        return None, None, None
    p = successes / total
    centre = p + Z * Z / (2 * total)
    margin = Z * math.sqrt(p * (1 - p) / total + Z * Z / (4 * total * total))
    scale = 1 + Z * Z / total
    low = 0.0 if successes == 0 else max(0.0, (centre - margin) / scale)
    high = 1.0 if successes == total else min(1.0, (centre + margin) / scale)
    return p, low, high


def summarize(rows: Iterable[dict]) -> dict[str, list[dict]]:
    """rows: run_id, verdict, unit, causes, flagged -> by_verdict, by_cause, edges, matrix."""
    rows = list(rows)
    by_verdict, by_cause, edges = [], [], []
    matrix: dict[tuple[str, str], int] = defaultdict(int)
    for verdict, spec in VERDICTS.items():
        units_ = [r for r in rows if r["verdict"] == verdict]
        flagged = [r for r in units_ if r["flagged"]]
        right = [r for r in flagged if any(matches(c, spec["allowed"]) or spec["edges"].get(c) is True
                                           for c in [*r["causes"], *r.get("context", [])])]
        positives = [r for r in units_ if any(matches(c, spec["targets"]) for c in r["causes"])]
        found = [r for r in positives if r["flagged"]]
        precision, plow, phigh = wilson(len(right), len(flagged))
        recall, rlow, rhigh = wilson(len(found), len(positives))
        by_verdict.append({"verdict": verdict, "scope": spec["scope"], "units": len(units_), "flagged": len(flagged),
                           "flagged_with_cause": len(right), "positives": len(positives), "detected": len(found),
                           "precision": precision, "precision_low": plow, "precision_high": phigh,
                           "recall": recall, "recall_low": rlow, "recall_high": rhigh})
        causes = sorted({c for r in positives for c in r["causes"] if matches(c, spec["targets"])})
        for cause in causes:
            with_cause = [r for r in units_ if cause in r["causes"]]
            hit = sum(r["flagged"] for r in with_cause)
            recall, rlow, rhigh = wilson(hit, len(with_cause))
            by_cause.append({"verdict": verdict, "cause": cause, "positives": len(with_cause), "detected": hit,
                             "recall": recall, "recall_low": rlow, "recall_high": rhigh})
        for edge, expected in spec["edges"].items():
            at = [r for r in units_ if edge in r["causes"]]
            if at:
                as_expected = sum(r["flagged"] == expected for r in at)
                edges.append({"verdict": verdict, "edge": edge, "expected_flagged": expected, "units": len(at),
                              "as_expected": as_expected})
        for r in flagged:
            for cause in r["causes"] or ["clean"]:
                matrix[(cause, verdict)] += 1
    return {"by_verdict": by_verdict, "by_cause": by_cause, "edges": edges,
            "matrix": [{"cause": c, "verdict": v, "units": n} for (c, v), n in sorted(matrix.items())]}


def gate(summary: dict[str, list[dict]]) -> list[str]:
    """Precision 1 and recall 1 for every verdict, every edge as expected. A verdict with no
    positive unit is not measured (the report says so); it does not break the gate."""
    problems = []
    for r in summary["by_verdict"]:
        if r["flagged"] and r["precision"] != 1:
            problems.append(f"{r['verdict']}: precision {r['flagged_with_cause']}/{r['flagged']}")
        if r["positives"] and r["recall"] != 1:
            problems.append(f"{r['verdict']}: recall {r['detected']}/{r['positives']}")
    for e in summary["edges"]:
        if e["as_expected"] != e["units"]:
            problems.append(f"{e['verdict']} at {e['edge']}: {e['as_expected']}/{e['units']} as expected")
    return problems


def _pct(p, low, high) -> str:
    return "n/a" if p is None else f"{100 * p:.1f} % [{100 * low:.0f}–{100 * high:.0f}]"


def markdown(name: str, runs: list[str], summary: dict[str, list[dict]], problems: list[str]) -> str:
    lines = [f"# Operational detection report: {name}", "",
             f"Runs: {', '.join(f'`{r}`' for r in runs)}. Intervals: 95 % Wilson. A flag is right when its unit has "
             "an allowed cause; recall is over the units with a target cause (docs/operational-report.md).", "",
             "## By verdict", "",
             "| Verdict | Unit | Units | Flagged | Precision | Positives | Recall |", "|---|---|---|---|---|---|---|"]
    for r in summary["by_verdict"]:
        lines.append(f"| `{r['verdict']}` | {r['scope']} | {r['units']} | {r['flagged']} "
                     f"| {_pct(r['precision'], r['precision_low'], r['precision_high'])} | {r['positives']} "
                     f"| {_pct(r['recall'], r['recall_low'], r['recall_high'])} |")
    lines += ["", "## By cause", "", "| Verdict | Cause | Units | Flagged | Recall |", "|---|---|---|---|---|"]
    for r in summary["by_cause"]:
        lines.append(f"| `{r['verdict']}` | `{r['cause']}` | {r['positives']} | {r['detected']} "
                     f"| {_pct(r['recall'], r['recall_low'], r['recall_high'])} |")
    lines += ["", "## Edges", "", "Units built exactly at a threshold.", "",
              "| Verdict | Edge | Expected | Units | As expected |", "|---|---|---|---|---|"]
    for e in summary["edges"]:
        lines.append(f"| `{e['verdict']}` | `{e['edge'][5:]}` | {'flagged' if e['expected_flagged'] else 'not flagged'} "
                     f"| {e['units']} | {e['as_expected']} |")
    verdicts = list(VERDICTS)
    causes = sorted({c["cause"] for c in summary["matrix"]})
    counts = {(c["cause"], c["verdict"]): c["units"] for c in summary["matrix"]}
    lines += ["", "## What flagged what", "", "Flagged units per cause (rows) and verdict (columns); a unit with "
              "several causes counts in each.", "",
              "| Cause | " + " | ".join(f"`{v}`" for v in verdicts) + " |", "|---|" + "---|" * len(verdicts)]
    for cause in causes:
        lines.append(f"| `{cause}` | " + " | ".join(str(counts.get((cause, v), "")) for v in verdicts) + " |")
    lines += ["", "## Gate", ""] + ([f"- {p}" for p in problems] if problems else
                                    ["All invariants hold: precision and recall 1 for every verdict, every edge as expected."])
    return "\n".join(lines) + "\n"
