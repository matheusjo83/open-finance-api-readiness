"""Fixtures: a local Spark session and the validation runs exported by the validator's tests
(make business-test runs services/validator/tests/export_runs.py into tests/.runs first)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

JOBS = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(JOBS / "business"), str(JOBS / "lib")]

from pyspark.sql import SparkSession  # noqa: E402

from rules import CONTRACT_SCHEMA, EXCHANGE_SCHEMA, build  # noqa: E402

RUNS = Path(os.environ.get("RUNS_DIR", JOBS / "tests" / ".runs"))


@pytest.fixture(scope="session")
def spark():
    if not (RUNS / "runs.json").exists():
        pytest.exit(f"{RUNS} has no exported runs: run `make business-test` at the repository root", returncode=2)
    session = (SparkSession.builder.master("local[2]").appName("business-tests")
               .config("spark.sql.shuffle.partitions", "2").config("spark.ui.enabled", "false")
               .config("spark.sql.session.timeZone", "UTC").getOrCreate())
    session.sparkContext.setLogLevel("ERROR")
    yield session
    session.stop()


class Run:
    def __init__(self, spark, name: str) -> None:
        source = f"{RUNS}/{{}}/run={name}/"
        self.exchanges = spark.read.schema(EXCHANGE_SCHEMA).json(source.format("exchanges"))
        self.contract = spark.read.schema(CONTRACT_SCHEMA).json(source.format("contract"))
        self.labels = spark.read.json(source.format("labels"))
        self.tables = build(self.exchanges, self.contract)
        self.checks = [r.asDict() for r in self.tables["business_checks"].collect()]
        self.label_of = {r["interaction_id"]: r.asDict() for r in self.labels.collect()}


@pytest.fixture(scope="session")
def runs(spark):
    cache: dict[str, Run] = {}

    def get(name: str) -> Run:
        if name not in cache:
            cache[name] = Run(spark, name)
        return cache[name]

    return get
