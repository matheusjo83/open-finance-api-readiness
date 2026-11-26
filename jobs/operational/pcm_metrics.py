"""Operational metrics from the PCM reports of both sides (docs/operational.md, item 16).

The reports are paired as the PCM pairs them: by fapiInteractionId, then on clientOrgId,
serverOrgId, statusCode, httpMethod and endpoint. A side counts when it was sent within 7 days
of the transaction (for the reporting metric at 08:00, by 08:00 of the next day). The
institution's view of an exchange is the client's report, or the server's when the client's is
missing; PAIRED_INCONSISTENT exchanges are left out of the response time, availability and
overload.

- per endpoint and day (Brasília): the P95 of the response time and the minutes available, with
  the 90-day moving availability;
- per institution and day: the share of 529;
- per API and day: the exchanges unpaired at 08:00 of the next day and at 7 days, and the
  inconsistent ones;
- per month, from the days: the monthly verdicts of the manual.

Every threshold comes from specs/operational/requirements.json.
"""

from __future__ import annotations

from pyspark.sql import DataFrame, SparkSession, Window
from pyspark.sql import functions as F
from pyspark.sql import types as T

S, I, D = T.StringType(), T.IntegerType(), T.DoubleType()
REPORT = T.StructType([
    T.StructField("role", S), T.StructField("endpoint", S), T.StructField("httpMethod", S),
    T.StructField("statusCode", I), T.StructField("clientOrgId", S), T.StructField("serverOrgId", S),
    T.StructField("fapiInteractionId", S), T.StructField("timestamp", S), T.StructField("processTimespan", D),
])
LINE = T.StructType([T.StructField("run_id", S), T.StructField("sent_at", S), T.StructField("report", REPORT)])
PAIRING_FIELDS = ["clientOrgId", "serverOrgId", "statusCode", "httpMethod", "endpoint"]


def _side(df: DataFrame, prefix: str) -> DataFrame:
    r = "report."
    return df.select(
        F.col("run_id").alias(f"{prefix}run_id"), F.col(r + "fapiInteractionId").alias("id"),
        F.to_timestamp("sent_at").alias(f"{prefix}sent"), F.to_timestamp(F.col(r + "timestamp")).alias(f"{prefix}ts"),
        F.col(r + "processTimespan").alias(f"{prefix}ms"),
        *[F.col(r + f).alias(prefix + f) for f in PAIRING_FIELDS])


def pairs(client: DataFrame, server: DataFrame, tz: str) -> DataFrame:
    """One row per exchange: both sides, the pairing at 7 days and at 08:00, and the view."""
    j = _side(client, "c_").join(_side(server, "s_"), "id", "full_outer")
    ts = F.coalesce("c_ts", "s_ts")
    day = F.to_date(F.from_utc_timestamp(ts, tz))
    cut_8h = F.to_utc_timestamp(F.date_add(day, 1).cast("timestamp") + F.expr("INTERVAL 8 HOURS"), tz)
    cut_7d = ts + F.expr("INTERVAL 7 DAYS")
    j = (j.withColumn("run_id", F.coalesce("c_run_id", "s_run_id")).withColumn("day", day)
         .withColumn("c_7d", F.col("c_sent").isNotNull() & (F.col("c_sent") <= cut_7d))
         .withColumn("s_7d", F.col("s_sent").isNotNull() & (F.col("s_sent") <= cut_7d))
         .withColumn("c_8h", F.col("c_sent").isNotNull() & (F.col("c_sent") <= cut_8h))
         .withColumn("s_8h", F.col("s_sent").isNotNull() & (F.col("s_sent") <= cut_8h)))
    same = F.lit(True)
    for f in PAIRING_FIELDS:
        same = same & F.col(f"c_{f}").eqNullSafe(F.col(f"s_{f}"))
    j = j.withColumn("pairing", F.when(F.col("c_7d") & F.col("s_7d"), F.when(same, "PAIRED").otherwise("PAIRED_INCONSISTENT"))
                     .when(F.col("c_7d") | F.col("s_7d"), "UNPAIRED"))
    # The institution's view: the client's report, the server's when the client's is missing.
    view = {f: F.when(F.col("c_7d"), F.col(f"c_{f}")).otherwise(F.col(f"s_{f}")) for f in PAIRING_FIELDS + ["ts", "ms"]}
    j = j.select("*", *[v.alias(f"v_{f}") for f, v in view.items()])
    local = F.from_utc_timestamp("v_ts", tz)
    return (j.withColumn("v_day", F.to_date(local)).withColumn("v_minute", F.date_format(local, "HH:mm"))
            .withColumn("api", F.regexp_extract(F.coalesce("c_endpoint", "s_endpoint"), r"^/open-banking/([^/]+)/", 1)))


def _requirements_df(spark: SparkSession, req: dict) -> DataFrame:
    rows = [(api["base_path"] + op.split(" ", 1)[1], op.split(" ", 1)[0], values["p95_ms"])
            for api in req["endpoints"]["apis"].values() for op, values in api["operations"].items()]
    return spark.createDataFrame(rows, "endpoint string, httpMethod string, sla_ms int")


def _half_up(col, share: float):
    """round(share * n) half up, in integers: share as hundredths."""
    hundredths = round(share * 100)
    return F.floor((F.lit(hundredths) * col + F.lit(50)) / F.lit(100)).cast("int")


def build(spark: SparkSession, client: DataFrame, server: DataFrame, req: dict) -> dict[str, DataFrame]:
    rt, av, ov, rep = req["response_time"], req["availability"], req["overload"], req["pcm_reporting"]
    p = pairs(client, server, av["timezone"]).cache()
    counted = p.where(F.col("pairing").isin("PAIRED", "UNPAIRED")).select(
        "run_id", F.col("v_serverOrgId").alias("serverOrgId"), F.col("v_endpoint").alias("endpoint"),
        F.col("v_httpMethod").alias("httpMethod"), F.col("v_day").alias("day"), F.col("v_minute").alias("minute"),
        F.col("v_statusCode").alias("status"), F.col("v_ms").alias("ms"))
    status = F.col("status")
    success = status.between(200, 299) | (status == 422)
    error = status.between(500, 599) | (status == 408)
    keys = ["run_id", "serverOrgId", "endpoint", "httpMethod", "day"]

    # Response time: the value at rank round(0.95 n) of the day's ascending times.
    times = counted.where(~status.isin(*rt["excluded_status"]) & (F.col("ms") > rt["min_process_timespan_ms_exclusive"]))
    w = Window.partitionBy(*keys)
    ranked = (times.withColumn("n_rt", F.count("*").over(w))
              .withColumn("rank", F.row_number().over(w.orderBy("ms")))
              .withColumn("i95", F.greatest(F.lit(1), _half_up(F.col("n_rt"), rt["percentile"]))))
    p95 = ranked.where(F.col("rank") == F.col("i95")).select(*keys, "n_rt", F.col("ms").cast("long").alias("p95_ms"))

    # Availability: minutes with valid requests, available at the minimum share of successes.
    minutes = (counted.where(success | error).groupBy(*keys, "minute")
               .agg(F.sum(success.cast("int")).alias("ok"), F.sum(error.cast("int")).alias("err"))
               .withColumn("available", F.col("ok") / (F.col("ok") + F.col("err")) >= av["minute_available_min"]))
    avail = (minutes.groupBy(*keys).agg(F.sum(F.col("available").cast("int")).alias("minutes_available"),
                                        F.sum((~F.col("available")).cast("int")).alias("minutes_unavailable"))
             .withColumn("daily_availability",
                         F.col("minutes_available") / (F.col("minutes_available") + F.col("minutes_unavailable"))))
    valid = counted.groupBy(*keys).agg(F.sum((success | error).cast("int")).alias("valid"),
                                        F.sum((status == ov["status"]).cast("int")).alias("n529"))

    # Calls of the endpoint on its day, every exchange counted once (pairing as at 7 days).
    ep_of = F.coalesce("c_endpoint", "s_endpoint")
    calls = (p.where(F.col("pairing").isNotNull())
             .groupBy("run_id", F.coalesce("c_serverOrgId", "s_serverOrgId").alias("serverOrgId"),
                      ep_of.alias("endpoint"), F.coalesce("c_httpMethod", "s_httpMethod").alias("httpMethod"), "day")
             .agg(*_reporting_counts()))

    sla = _requirements_df(spark, req)
    endpoint_day = (calls.join(p95, keys, "left").join(avail, keys, "left").join(valid, keys, "left")
                    .join(sla, ["endpoint", "httpMethod"], "left")
                    .fillna(0, ["n_rt", "valid", "n529"])
                    .withColumn("rt_ok", F.when(F.col("p95_ms").isNotNull(), F.col("p95_ms") <= F.col("sla_ms")))
                    .withColumn("availability_ok", F.when(F.col("daily_availability").isNotNull(),
                                                          F.col("daily_availability") >= av["daily_min"])))
    # 90-day moving availability, over the days it is defined.
    days = F.datediff("day", F.lit("1970-01-01")).cast("long")
    long_w = (Window.partitionBy("run_id", "serverOrgId", "endpoint", "httpMethod").orderBy(days)
              .rangeBetween(-(av["long_window_days"] - 1), 0))
    endpoint_day = (endpoint_day.withColumn("long_availability", F.avg("daily_availability").over(long_w))
                    .withColumn("long_ok", F.col("long_availability") >= av["long_min"]))

    institution_day = (valid.groupBy("run_id", "serverOrgId", "day").agg(F.sum("valid").alias("valid"), F.sum("n529").alias("n529"))
                       .withColumn("share_529", F.when(F.col("valid") > 0, F.col("n529") / F.col("valid")))
                       .withColumn("overload_ok", F.col("share_529") < ov["daily_max_share_exclusive"])
                       .withColumn("overload_over_5", F.col("share_529") > ov["month"]["other_days_max_share"]))

    api_day = (p.where(F.col("pairing").isNotNull())
               .groupBy("run_id", F.coalesce("c_serverOrgId", "s_serverOrgId").alias("serverOrgId"), "api", "day")
               .agg(*_reporting_counts())
               .withColumn("share_8h", F.col("unpaired_8h") / F.col("calls"))
               .withColumn("share_7d", (F.col("unpaired_7d") + F.col("inconsistent")) / F.col("calls"))
               .withColumn("reporting_8h_ok", F.col("share_8h") <= 1 - rep["next_day_8h_min_share"] + 1e-12)
               .withColumn("reporting_7d_ok", F.col("share_7d") <= 1 - rep["seven_days_min_share"] + 1e-12))

    return {"pairs": p, "endpoint_day": endpoint_day, "institution_day": institution_day, "api_day": api_day,
            **months(endpoint_day, institution_day, api_day, req)}


def _reporting_counts():
    both_8h = F.col("c_8h") & F.col("s_8h")
    return [F.count("*").alias("calls"),
            F.sum((~both_8h).cast("int")).alias("unpaired_8h"),
            F.sum((F.col("pairing") == "UNPAIRED").cast("int")).alias("unpaired_7d"),
            F.sum((F.col("pairing") == "PAIRED_INCONSISTENT").cast("int")).alias("inconsistent")]


def months(endpoint_day: DataFrame, institution_day: DataFrame, api_day: DataFrame, req: dict) -> dict[str, DataFrame]:
    """The monthly verdicts (Manual de APIs 5.3.3, 5.1.2, 5.4.1; Manual de Monitoramento 2.4).
    A month is the days of it in the data; `days` says how many."""
    rt, ov, rep = req["response_time"]["month"], req["overload"]["month"], req["pcm_reporting"]
    month = F.date_format("day", "yyyy-MM")

    # Response time: within the SLA on round(90 %) of the days with requests, the others at most 20 % over.
    rt_days = endpoint_day.where(F.col("p95_ms").isNotNull())
    rt_month = (rt_days.groupBy("run_id", "serverOrgId", "endpoint", "httpMethod", month.alias("month"))
                .agg(F.count("*").alias("days"), F.sum(F.col("rt_ok").cast("int")).alias("days_ok"),
                     F.max(F.col("p95_ms") / F.col("sla_ms")).alias("worst_factor"))
                .withColumn("rt_month_ok", (F.col("days_ok") >= _half_up(F.col("days"), rt["min_share_of_days"]))
                            & (F.col("worst_factor") <= rt["other_days_max_factor"])))
    # Availability: every day within, and the long availability of the month's last day.
    last = Window.partitionBy("run_id", "serverOrgId", "endpoint", "httpMethod", "month").orderBy(F.col("day").desc())
    av_month = (endpoint_day.where(F.col("daily_availability").isNotNull()).withColumn("month", month)
                .withColumn("last", F.row_number().over(last) == 1)
                .groupBy("run_id", "serverOrgId", "endpoint", "httpMethod", "month")
                .agg(F.min(F.col("availability_ok").cast("int")).cast("boolean").alias("all_days_ok"),
                     F.max(F.when(F.col("last"), F.col("long_availability"))).alias("long_availability"),
                     F.max(F.when(F.col("last"), F.col("long_ok"))).alias("long_ok"))
                .withColumn("availability_month_ok", F.col("all_days_ok") & F.col("long_ok")))
    endpoint_month = rt_month.join(av_month, ["run_id", "serverOrgId", "endpoint", "httpMethod", "month"], "full_outer")

    institution_month = (institution_day.where(F.col("share_529").isNotNull())
                         .groupBy("run_id", "serverOrgId", month.alias("month"))
                         .agg(F.count("*").alias("days"), F.sum(F.col("overload_ok").cast("int")).alias("days_ok"),
                              F.max("share_529").alias("worst_share_529"))
                         .withColumn("overload_month_ok",
                                     (F.col("days_ok") >= _half_up(F.col("days"), ov["min_share_of_days"]))
                                     & (F.col("worst_share_529") <= ov["other_days_max_share"])))

    api_month = (api_day.groupBy("run_id", "serverOrgId", "api", month.alias("month"))
                 .agg(F.count("*").alias("days"), F.sum((~F.col("reporting_8h_ok")).cast("int")).alias("days_8h_missed"),
                      F.min(F.col("reporting_7d_ok").cast("int")).cast("boolean").alias("all_7d_ok"))
                 .withColumn("reporting_month_ok",
                             (F.col("days_8h_missed") <= rep["max_missed_days_per_month"]) & F.col("all_7d_ok")))
    return {"endpoint_month": endpoint_month, "institution_month": institution_month, "api_month": api_month}
