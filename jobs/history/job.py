"""History job: a seeded history of PCM reports of both sides, with operational defects built in
(generator.py, docs/history.md), written as the live telemetry is, with its labels.

- <source>/pcm/client/run=<id>/ and <source>/pcm/server/run=<id>/: the reports, one JSON line
  each, as the validator and the proxy write them (docs/telemetry.md);
- <source>/pcm-labels/run=<id>/<scope>/: the ground truth per endpoint and day, per endpoint
  (the 90-day availability at the last day), per API and day (reporting) and per day (overload).

Usage: job.py [--seed 1] [--end 2026-11-15] [--days 90] [--minutes 30] [--per-minute 3]
              [--rates JSON] [--source s3a://lakehouse] [--requirements PATH] [--partitions N]
The run id says what was generated: history-s<seed>-<days>d-<minutes>x<per minute>.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

from pyspark import StorageLevel
from pyspark.sql import DataFrame, SparkSession
from pyspark.sql import functions as F

import generator
from generator import DEFAULT_RATES, Settings, chronic_days, endpoints, generate

REQUIREMENTS = "/opt/specs/operational/requirements.json"


def labels(ledgers: DataFrame, s: Settings) -> dict[str, DataFrame]:
    """The ground truth per scope, from the units' ledgers and the requirements' thresholds."""
    defined = F.col("minutes_available") + F.col("minutes_unavailable")
    day = (ledgers
           .withColumn("daily_availability", F.when(defined > 0, F.col("minutes_available") / defined))
           .withColumn("rt_ok", F.when(F.col("p95_ms").isNotNull(), F.col("p95_ms") <= F.col("sla_ms")))
           .withColumn("availability_ok", F.when(defined > 0, F.col("daily_availability") >= 0.95)))
    long = (day.where(F.col("daily_availability").isNotNull())
            .groupBy("run_id", "api", "method", "endpoint")
            .agg(F.avg("daily_availability").alias("long_availability"), F.max("day").alias("day"),
                 F.max(F.col("chronic").cast("int")).cast("boolean").alias("chronic"),
                 F.count("*").alias("days"))
            .withColumn("long_ok", F.col("long_availability") >= 0.995))
    sums = [F.sum(c).alias(c) for c in ("calls", "unpaired_8h", "unpaired_7d", "inconsistent", "valid", "n529")]
    api = (day.groupBy("run_id", "api", "day")
           .agg(*sums, *[F.max(F.when(F.col("scope") == "api", F.col(c))).alias(c) for c in ("class", "variant")])
           .withColumn("share_8h", F.col("unpaired_8h") / F.col("calls"))
           .withColumn("share_7d", (F.col("unpaired_7d") + F.col("inconsistent")) / F.col("calls"))
           .withColumn("reporting_8h_ok", F.col("share_8h") <= 0.05)
           .withColumn("reporting_7d_ok", F.col("share_7d") <= 0.01))
    institution = (day.groupBy("run_id", "day")
                   .agg(*sums, *[F.max(F.when(F.col("scope") == "institution", F.col(c))).alias(c)
                                 for c in ("class", "variant", "edge")])
                   .withColumn("share_529", F.col("n529") / F.col("valid"))
                   .withColumn("overload_ok", F.col("share_529") < 0.005)
                   .withColumn("overload_over_5", F.col("share_529") > 0.05))
    return {"endpoint-day": day, "endpoint": long, "api-day": api, "institution-day": institution}


def main(argv: list[str] | None = None, spark: SparkSession | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--end", default="2026-11-15")
    parser.add_argument("--days", type=int, default=90)
    parser.add_argument("--minutes", type=int, default=30)
    parser.add_argument("--per-minute", type=int, default=3)
    parser.add_argument("--rates", default="")
    parser.add_argument("--source", default="s3a://lakehouse")
    parser.add_argument("--requirements", default=REQUIREMENTS)
    parser.add_argument("--partitions", type=int, default=8)
    args = parser.parse_args(argv)

    rates = {**DEFAULT_RATES, **(json.loads(args.rates) if args.rates else {})}
    s = Settings(seed=args.seed, end=date.fromisoformat(args.end), days=args.days, minutes=args.minutes,
                 per_minute=args.per_minute, rates=rates)
    with open(args.requirements, encoding="utf-8") as f:
        eps = endpoints(json.load(f))
    chronic = {ep.key: chronic_days(s, ep) for ep in eps}

    own = spark is None
    if own:
        from lakehouse import session
        spark = session("history")
        spark.sparkContext.setLogLevel("WARN")
        # The units are generated in the executors' Python, which deps.pyFiles (local://) leaves
        # out: ship the module to them.
        spark.sparkContext.addPyFile(generator.__file__)
    units = [(i, day) for i in range(len(eps)) for day in s.day_list()]
    lines = (spark.sparkContext.parallelize(units, args.partitions)
             .flatMap(lambda u: generate(s, eps, eps[u[0]], u[1], chronic[eps[u[0]].key]))
             .persist(StorageLevel.MEMORY_AND_DISK))

    run = f"run={s.run_id}"
    counts = {}
    for kind in ("client", "server"):
        part = lines.filter(lambda x, k=kind: x[0] == k).map(lambda x: (x[1],))
        spark.createDataFrame(part, "value string").write.mode("overwrite").text(f"{args.source}/pcm/{kind}/{run}/")
    ledgers = spark.read.json(lines.filter(lambda x: x[0] == "ledger").map(lambda x: x[1])).cache()
    for scope, df in labels(ledgers, s).items():
        df.write.mode("overwrite").json(f"{args.source}/pcm-labels/{run}/{scope}/")
    for kind in ("client", "server", "ledger"):
        counts[kind] = lines.filter(lambda x, k=kind: x[0] == k).count()
    episodes = {f"{r['class'] or r['edge']}/{r['variant'] or ''}".rstrip("/"): r["count"]
                for r in ledgers.where(F.col("class").isNotNull() | F.col("edge").isNotNull())
                .groupBy("class", "variant", "edge").count().orderBy("class", "variant", "edge").collect()}
    chronic_endpoints = sorted(k for k, v in chronic.items() if v)
    print("HISTORY " + json.dumps({"run_id": s.run_id, "reports": {"client": counts["client"], "server": counts["server"]},
                                   "units": counts["ledger"], "episodes": episodes,
                                   "chronic_endpoints": chronic_endpoints}), flush=True)
    lines.unpersist()
    if own:
        spark.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
