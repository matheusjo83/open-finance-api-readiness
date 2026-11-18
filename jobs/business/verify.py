"""Test harness, not part of the business layer: compares a run's business checks with the
proxy's labels (ground truth). The rules never see the labels; this runs after they are written
(job.py --verify-labels, make business-smoke) and in the tests.

Expected: every exchange with a finding carries a business defect, and every business defect is
found unless the rule that should see it was inconclusive (its inputs were excluded by the
contract layer or incomplete).
"""

from __future__ import annotations

from pyspark.sql import DataFrame
from pyspark.sql import functions as F

LABEL_COLUMNS = ["interaction_id", "defect_class", "defect_layer"]


def compare(exchanges: DataFrame, checks: DataFrame, labels: DataFrame) -> tuple[dict, list[dict]]:
    """(outcome per class, the exchanges that disagree with their label)."""
    per_exchange = checks.groupBy("interaction_id").agg(
        F.max(F.col("outcome") == "finding").alias("flagged"),
        F.max(F.col("outcome") == "inconclusive").alias("inconclusive"),
        F.sort_array(F.collect_set(F.when(F.col("outcome") == "finding", F.col("rule")))).alias("rules"))
    joined = (exchanges.select("interaction_id", "path").join(labels.select(*LABEL_COLUMNS), "interaction_id", "left")
              .join(per_exchange, "interaction_id", "left")
              .fillna({"flagged": False, "inconclusive": False})
              .withColumn("business", F.coalesce(F.col("defect_layer") == "business", F.lit(False)))
              .withColumn("label", F.coalesce("defect_class", F.lit("clean")))
              .withColumn("result", F.when(F.col("flagged"), "flagged")
                          .when(F.col("inconclusive"), "inconclusive").otherwise("passed")))
    by_class: dict[str, dict[str, int]] = {}
    for row in joined.groupBy("label", "result").count().collect():
        by_class.setdefault(row["label"], {})[row["result"]] = row["count"]
    wrong = joined.where((F.col("flagged") & ~F.col("business"))
                         | (F.col("business") & ~F.col("flagged") & ~F.col("inconclusive")))
    return dict(sorted(by_class.items())), [r.asDict() for r in wrong.select("label", "path", "rules").limit(20).collect()]
