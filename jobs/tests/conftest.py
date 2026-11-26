"""Fixtures: a local Spark session and the validation runs exported by the validator's tests
(make jobs-test runs services/validator/tests/export_runs.py into tests/.runs first)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

JOBS = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(JOBS / "business"), str(JOBS / "report"), str(JOBS / "history"), str(JOBS / "lib")]

from pyspark.sql import SparkSession  # noqa: E402

from rules import CONTRACT_SCHEMA, EXCHANGE_SCHEMA, build  # noqa: E402

RUNS = Path(os.environ.get("RUNS_DIR", JOBS / "tests" / ".runs"))


@pytest.fixture(scope="session")
def spark():
    if not (RUNS / "runs.json").exists():
        pytest.exit(f"{RUNS} has no exported runs: run `make jobs-test` at the repository root", returncode=2)
    session = (SparkSession.builder.master("local[2]").appName("jobs-tests")
               .config("spark.sql.shuffle.partitions", "2").config("spark.ui.enabled", "false")
               # The session caches the tables of every exported run (eight since the renewal
               # classes); the default 1g heap ran out on a CI runner (decision 38).
               .config("spark.driver.memory", "2g")
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
        # Cached: the tests run many actions over the same small tables.
        self.tables = {name: df.cache() for name, df in build(self.exchanges, self.contract).items()}
        self.labels = self.labels.cache()
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


@pytest.fixture(scope="session")
def history(spark, tmp_path_factory):
    """A small history (seed 1, 20 minutes x 1 request), generated once for the session by the
    history job: (source, run id). Seed 1 has a month failed by every verdict."""
    import importlib.util

    source = tmp_path_factory.mktemp("history")
    # The executors' Python gets the module as in the cluster.
    spark.sparkContext.addPyFile(str(JOBS / "history" / "generator.py"))
    # By path: job.py exists in several jobs.
    spec = importlib.util.spec_from_file_location("history_job_main", JOBS / "history" / "job.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.main(["--seed", "1", "--minutes", "20", "--per-minute", "1", "--source", f"file://{source}",
                        "--requirements", str(JOBS.parent / "specs/operational/requirements.json"),
                        "--partitions", "4"], spark=spark) == 0
    return f"file://{source}", "history-s1-90d-20x1"

