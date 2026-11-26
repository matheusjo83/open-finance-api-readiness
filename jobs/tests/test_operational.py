"""The operational job (docs/operational-job.md): over a generated history it reproduces every
label exactly, its monthly verdicts follow from the labelled days, and over the live runs every
exchange is paired."""

from __future__ import annotations

import importlib.util
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import pytest

JOBS = Path(__file__).resolve().parents[1]
REQUIREMENTS = JOBS.parent / "specs/operational/requirements.json"
RUNS = JOBS / "tests" / ".runs"


def load(name: str, path: Path):
    """Modules by path: job.py and metrics.py exist in several jobs."""
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


sys.path.insert(0, str(JOBS / "operational"))
operational_job = load("operational_job", JOBS / "operational" / "job.py")
detection = load("detection", JOBS / "operational" / "detection.py")


def test_reproduces_every_label(spark, history, capsys):
    source, run_id = history
    assert operational_job.main(["--run-id", run_id, "--source", source, "--requirements", str(REQUIREMENTS),
                                 "--check", "--no-tables"], spark=spark) == 0
    out = json.loads(capsys.readouterr().out.split("OPERATIONAL ", 1)[1])
    assert out["check"] == "ok"
    assert set(out["pairing"]) == {"PAIRED", "UNPAIRED", "PAIRED_INCONSISTENT"}
    assert all(out["verdicts"][k] > 0 for k in ("endpoint_days_out_of_sla", "endpoint_days_unavailable",
                                                "institution_days_overloaded", "api_days_8h_missed", "api_days_7d_missed"))


def test_check_notices_a_wrong_label(spark, history, tmp_path):
    """--check is not a formality: one label changed, one difference reported."""
    source, run_id = history
    copy = tmp_path / "copy"
    for side in ("client", "server"):
        spark.read.text(f"{source}/pcm/{side}/run={run_id}/").write.text(f"file://{copy}/pcm/{side}/run={run_id}/")
    for scope in ("endpoint", "api-day", "institution-day"):
        spark.read.json(f"{source}/pcm-labels/run={run_id}/{scope}/").write.json(f"file://{copy}/pcm-labels/run={run_id}/{scope}/")
    days = spark.read.json(f"{source}/pcm-labels/run={run_id}/endpoint-day/").collect()
    rows = [r.asDict() for r in days]
    target = next(r for r in rows if r["p95_ms"])
    target["p95_ms"] += 1
    spark.createDataFrame([json.dumps(r) for r in rows], "string").write.text(f"file://{copy}/pcm-labels/run={run_id}/endpoint-day/")
    tables = operational_job.build(spark, *operational_job.read_run(spark, f"file://{copy}", run_id),
                                   json.loads(REQUIREMENTS.read_text()))
    problems = operational_job.compare(spark, tables, f"file://{copy}", run_id)
    assert len(problems) == 1 and "p95_ms" in problems[0], problems


def _half_up(x: float) -> int:
    return math.floor(round(x, 9) + 0.5)


def test_months_follow_from_the_labelled_days(spark, history):
    """The monthly verdicts, recomputed in plain Python from the labels' days."""
    source, run_id = history
    req = json.loads(REQUIREMENTS.read_text())
    tables = operational_job.build(spark, *operational_job.read_run(spark, source, run_id), req)
    read = lambda scope: [r.asDict() for r in spark.read.json(f"{source}/pcm-labels/run={run_id}/{scope}/").collect()]  # noqa: E731

    expected_rt, expected_av = {}, {}
    by_month = defaultdict(list)
    for r in read("endpoint-day"):
        by_month[(r["endpoint"], r["method"], r["day"][:7])].append(r)
    long_at = {}
    for key, rows in by_month.items():
        timed = [r for r in rows if r["p95_ms"] is not None]
        ok = sum(r["rt_ok"] for r in timed)
        expected_rt[key] = ok >= _half_up(0.9 * len(timed)) and max(r["p95_ms"] / r["sla_ms"] for r in timed) <= 1.2
        last = max(rows, key=lambda r: r["day"])
        # The long availability of the month's last day, from the labelled daily values.
        window = [r for (e, m, _), rs in by_month.items() if (e, m) == key[:2] for r in rs
                  if r["daily_availability"] is not None and r["day"] <= last["day"]]
        recent = sorted(window, key=lambda r: r["day"])[-90:]
        long_at[key] = sum(r["daily_availability"] for r in recent) / len(recent)
        expected_av[key] = all(r["availability_ok"] for r in rows) and long_at[key] >= 0.995
    got = {(r["endpoint"], r["httpMethod"], r["month"]): r for r in tables["endpoint_month"].collect()}
    assert {k: got[k]["rt_month_ok"] for k in expected_rt} == expected_rt
    assert {k: got[k]["availability_month_ok"] for k in expected_av} == expected_av
    assert all(math.isclose(got[k]["long_availability"], v) for k, v in long_at.items())

    days = defaultdict(list)
    for r in read("institution-day"):
        days[r["day"][:7]].append(r)
    expected = {m: sum(r["overload_ok"] for r in rs) >= _half_up(0.9 * len(rs)) and max(r["share_529"] for r in rs) <= 0.05
                for m, rs in days.items()}
    assert {r["month"]: r["overload_month_ok"] for r in tables["institution_month"].collect()} == expected

    api = defaultdict(list)
    for r in read("api-day"):
        api[(r["api"], r["day"][:7])].append(r)
    expected = {k: sum(not r["reporting_8h_ok"] for r in rs) <= 3 and all(r["reporting_7d_ok"] for r in rs)
                for k, rs in api.items()}
    assert {(r["api"], r["month"]): r["reporting_month_ok"] for r in tables["api_month"].collect()} == expected
    # The history has months that fail and months that pass, for each verdict.
    for table, column in (("endpoint_month", "rt_month_ok"), ("endpoint_month", "availability_month_ok"),
                          ("institution_month", "overload_month_ok"), ("api_month", "reporting_month_ok")):
        assert {r[column] for r in tables[table].collect()} == {True, False}, column


def test_live_runs_pair_completely(spark):
    """Over the validation runs the validator's tests export: every exchange PAIRED, and the
    endpoint-days add up to the exchanges."""
    req = json.loads(REQUIREMENTS.read_text())
    for name in ("clean", "default"):
        tables = operational_job.build(spark, *operational_job.read_run(spark, f"file://{RUNS}", name), req)
        pairing = {r["pairing"]: r["count"] for r in tables["pairs"].groupBy("pairing").count().collect()}
        labels = spark.read.json(f"{RUNS}/labels/run={name}/").count()
        assert pairing == {"PAIRED": labels}, name
        assert tables["endpoint_day"].agg({"calls": "sum"}).first()[0] == labels
        assert tables["endpoint_day"].where("p95_ms is null").count() == 0


def _line(role: str, interaction: str, ts: str, ms: float, status: int = 200, sent: str | None = None,
          endpoint: str = "/open-banking/accounts/v2/accounts/{accountId}/balances") -> dict:
    report = {"role": role, "endpoint": endpoint, "httpMethod": "GET", "statusCode": status,
              "clientOrgId": "c", "serverOrgId": "s", "fapiInteractionId": interaction, "timestamp": ts,
              "processTimespan": ms}
    return {"run_id": "synthetic", "sent_at": sent or ts, "report": report}


def _build(spark, calls: list[tuple]) -> dict:
    """calls: (day, hour, ms[, status, client sent_at]) -> the job's tables; both sides report."""
    client, server = [], []
    for i, call in enumerate(calls):
        day, hour, ms, *rest = call
        status, client_sent = (rest + [200, None])[:2] if len(rest) < 2 else rest
        ts = f"{day}T{hour:02d}:00:{i % 60:02d}.000Z"
        client.append(_line("CLIENT", f"00000000-0000-4000-8000-{i:012d}", ts, ms, status, client_sent))
        server.append(_line("SERVER", f"00000000-0000-4000-8000-{i:012d}", ts, ms, status))
    df = lambda rows: spark.createDataFrame([json.dumps(r) for r in rows], "string")  # noqa: E731
    read = lambda rows: spark.read.schema(operational_job.LINE).json(df(rows).rdd.map(lambda r: r[0]))  # noqa: E731
    return operational_job.build(spark, read(client), read(server), json.loads(REQUIREMENTS.read_text()))


def test_p95_rank_rounds_half_up(spark):
    """10 times on a day: round(9.5) is 10, the largest (the manual's index, half up)."""
    tables = _build(spark, [("2026-11-02", 15, ms) for ms in range(100, 1100, 100)])
    assert [r["p95_ms"] for r in tables["endpoint_day"].collect()] == [1000]


def test_month_needs_round_90_percent_of_days(spark):
    """31 days, 27 within the SLA and 4 between 1.0 and 1.2 times it: round(27.9) is 28 days,
    so the month fails; with 28 within, it passes."""
    def month(within: int):
        days = [f"2026-10-{d:02d}" for d in range(1, 32)]
        return _build(spark, [(day, 15, 1000 if i < within else 1700) for i, day in enumerate(days)])["endpoint_month"]
    assert [r["rt_month_ok"] for r in month(27).collect()] == [False]
    assert [r["rt_month_ok"] for r in month(28).collect()] == [True]


def test_reporting_month_allows_three_late_days(spark):
    """Days whose report came after 08:00 of the next day (Brasília) but within 7 days: three a
    month are allowed, four are not. A report at 07:59 of the next day is in time."""
    def month(late_days: int):
        calls = []
        for d in range(1, 11):
            day = f"2026-10-{d:02d}"
            sent = f"2026-10-{d + 1:02d}T11:30:00.000Z" if d <= late_days else f"2026-10-{d + 1:02d}T10:59:00.000Z"
            calls.append((day, 15, 100, 200, sent))
        return _build(spark, calls)
    three = month(3)
    assert sorted(r["reporting_8h_ok"] for r in three["api_day"].collect()) == [False] * 3 + [True] * 7
    assert [r["reporting_month_ok"] for r in three["api_month"].collect()] == [True]
    assert [r["reporting_month_ok"] for r in month(4)["api_month"].collect()] == [False]


def test_detection_report_holds_its_invariants(spark, history, capsys):
    """Over the history every verdict flags only units with an allowed cause, finds every unit
    with a target cause, and every edge comes out as the requirements say."""
    source, run_id = history
    assert operational_job.main(["--run-id", run_id, "--source", source, "--requirements", str(REQUIREMENTS),
                                 "--no-tables", "--report", "test", "--gate"], spark=spark) == 0
    out = capsys.readouterr().out
    assert "All invariants hold" in out
    summary = json.loads(out.split("OPERATIONAL_REPORT ", 1)[1])
    assert summary["gate"] == "ok" and set(summary["verdicts"]) == set(detection.VERDICTS)
    # Seed 1 exercises every verdict: a precision and a recall, both 1.
    assert all(v == [1.0, 1.0] for v in summary["verdicts"].values()), summary["verdicts"]


def test_detection_gate_notices_a_missed_or_wrong_flag(spark, history):
    """The gate is not a formality: a verdict that misses a target, flags a unit without an
    allowed cause or gets an edge wrong breaks it."""
    source, run_id = history
    req = json.loads(REQUIREMENTS.read_text())
    tables = operational_job.build(spark, *operational_job.read_run(spark, source, run_id), req)
    labels = {s: spark.read.json(f"{source}/pcm-labels/run={run_id}/{s}/") for s in ("endpoint-day", "api-day", "institution-day")}
    rows = [r.asDict() for r in detection.units(tables, labels, run_id).collect()]
    assert detection.gate(detection.summarize(rows)) == []

    def broken(pick, change) -> list[str]:
        changed = [dict(r) for r in rows]
        target = next(r for r in changed if pick(r))
        target.update(change(target))
        return detection.gate(detection.summarize(changed))

    assert any("recall" in p for p in broken(lambda r: r["verdict"] == "rt_day" and r["flagged"], lambda r: {"flagged": False}))
    assert any("precision" in p for p in broken(lambda r: r["verdict"] == "overload_day" and not r["causes"],
                                                lambda r: {"flagged": True}))
    assert any("edge" in p for p in broken(lambda r: r["verdict"] == "rt_day" and "edge:p95-at-sla" in r["causes"],
                                           lambda r: {"flagged": True}))
