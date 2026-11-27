"""The limits job (docs/limits-job.md) over hand-built client logs: each verdict at and just
inside its limit, the 60-second window, the Brasília month, what the monthly limit does not
count, and the detection report's gate."""

from __future__ import annotations

import importlib.util
import json
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

JOBS = Path(__file__).resolve().parents[1]
REQUIREMENTS = JOBS.parent / "specs/operational/requirements.json"
sys.path[:0] = [str(JOBS / "limits"), str(JOBS / "operational")]

import judge  # noqa: E402


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


limits_job = _load("limits_job", JOBS / "limits" / "job.py")
REQ = json.loads(REQUIREMENTS.read_text())
ACC, CON = "/open-banking/accounts/v2", "/open-banking/consents/v3"
T0 = int(datetime(2026, 11, 27, 12, 0, tzinfo=timezone.utc).timestamp() * 1000)


class Log:
    """A client log being built: one record per request, the k6 script's fields."""

    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.labels: list[dict] = []

    def add(self, case: str, path: str, status: int, start: int, *, kind: str = "operational", method: str = "GET",
            obj: str | None = "ACC-1", page: int = 1, end: int | None = None, defect: str | None = None) -> None:
        interaction = str(uuid.uuid4())
        self.rows.append({"run_id": "t", "case": case, "kind": kind, "route": "-", "origin": f"origin-{case}",
                          "object": obj, "page": page, "method": method, "path": path, "interaction": interaction,
                          "status": status, "start": start, "end": start + 2 if end is None else end})
        cls, _, variant = (defect or "").partition("/")
        self.labels.append({"run_id": "t", "interaction_id": interaction, "status": status,
                            "defect_class": cls or None, "defect_variant": variant or None})

    def write(self, root: Path) -> str:
        for name, rows in (("limits/client", self.rows), ("labels", self.labels)):
            folder = root / name / "run=t"
            folder.mkdir(parents=True)
            (folder / "part-00001.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
        return f"file://{root}"


def judged(spark, log: Log) -> dict[str, list[dict]]:
    """The judged requests per case, in the order they were sent."""
    df = judge.judge(spark.createDataFrame(log.rows, judge.CLIENT), REQ)
    out: dict[str, list[dict]] = {}
    for r in df.orderBy("start", "end").collect():
        out.setdefault(r["case"], []).append(r.asDict())
    return out


def verdicts(rows: list[dict]) -> list[str | None]:
    return [r["verdict"] for r in rows if r["status"] in (429, 423)]


def test_operations_cover_the_requirements():
    ops = judge.operations(REQ)
    assert len(ops) == 11 and {o["endpoint"] for o in ops if o["tpm"] is None} == {
        "POST /consents", "GET /consents/{consentId}", "DELETE /consents/{consentId}",
        "POST /consents/{consentId}/extends", "GET /consents/{consentId}/extensions"}


def test_traffic_at_the_limit_and_one_before(spark):
    log = Log()
    detail = f"{ACC}/accounts/ACC-1"  # TPM 1000
    for case, refused_at in (("at", 1000), ("early", 999)):
        for i in range(refused_at):
            log.add(case, detail, 200, T0 + i * 10, kind="traffic")
        log.add(case, detail, 429, T0 + refused_at * 10, kind="traffic")
    out = judged(spark, log)
    assert verdicts(out["at"]) == [None] and out["at"][-1]["tpm_before"] == 1000
    assert verdicts(out["early"]) == ["traffic_limit_early"]


def test_traffic_window_is_any_60_seconds(spark):
    """A sliding minute that allows the TPM passes across a calendar boundary; requests older
    than 60 seconds do not count; ties at one millisecond count in the order they ended."""
    log = Log()
    detail = f"{ACC}/accounts/ACC-1"
    for i in range(1000):  # 30 s before the boundary to 20 s after it
        log.add("sliding", detail, 200, T0 - 30_000 + i * 50, kind="traffic")
    log.add("sliding", detail, 429, T0 + 20_000, kind="traffic")
    for i in range(1000):  # all of them more than 60 s before the 429
        log.add("old", detail, 200, T0 + i, kind="traffic")
    log.add("old", detail, 429, T0 + 1000 + 60_001, kind="traffic")
    for i in range(1000):
        log.add("ties", detail, 200, T0, end=T0 + 1 + i, kind="traffic")
    log.add("ties", detail, 429, T0, end=T0 + 5000, kind="traffic")
    out = judged(spark, log)
    assert verdicts(out["sliding"]) == [None]
    assert verdicts(out["old"]) == ["traffic_limit_early"]
    assert verdicts(out["ties"]) == [None]


def test_monthly_counts_successes_of_first_pages_of_the_object(spark):
    log = Log()
    listing, detail = f"{ACC}/accounts", f"{ACC}/accounts/ACC-1"  # 8 calls a month
    t = T0
    for case, successes in (("at", 8), ("early", 7)):
        for _ in range(successes):
            log.add(case, detail, 200, t)
            t += 10
        log.add(case, detail, 423, t)
        t += 10
    # 7 counted successes; pages after the first, failures and another object do not count.
    for _ in range(7):
        log.add("not-counted", listing, 200, t, obj="consent-1")
        log.add("not-counted", f"{listing}?page=2&page-size=25", 200, t + 1, obj="consent-1", page=2)
        log.add("not-counted", f"{listing}?accountType=NENHUM", 400, t + 2, obj="consent-1")
        t += 10
    log.add("not-counted", listing, 200, t, obj="consent-2")
    log.add("not-counted", listing, 423, t + 10, obj="consent-1")
    # A page after the first refused with 423 is judged like any other.
    log.add("page-two", f"{listing}?page=2", 423, t + 20, obj="consent-1", page=2)
    out = judged(spark, log)
    assert verdicts(out["at"]) == [None] and out["at"][-1]["counted_before"] == 8
    assert verdicts(out["early"]) == ["operational_limit_early"]
    assert verdicts(out["not-counted"]) == ["operational_limit_early"]
    assert out["not-counted"][-1]["counted_before"] == 7
    assert verdicts(out["page-two"]) == ["operational_limit_early"]


def test_monthly_counts_the_brasilia_month(spark):
    """2026-12-01 01:00Z is still 30 November in Brasília (UTC-3)."""
    log = Log()
    detail = f"{ACC}/accounts/ACC-1"
    start = int(datetime(2026, 12, 1, 1, 0, tzinfo=timezone.utc).timestamp() * 1000)
    for i in range(8):
        log.add("same", detail, 200, start + i * 1000)
        log.add("next", detail, 200, start + i * 1000)
    log.add("same", detail, 423, start + 90 * 60_000)  # 23:30 in Brasília: November
    log.add("next", detail, 423, start + 150 * 60_000)  # 00:30 of 1 December: a new month
    out = judged(spark, log)
    assert verdicts(out["same"]) == [None]
    assert verdicts(out["next"]) == ["operational_limit_early"] and out["next"][-1]["counted_before"] == 0


def test_no_limit_on_consents(spark):
    log = Log()
    for status in (201, 429, 423):
        log.add("create", f"{CON}/consents", status, T0 + status, method="POST", obj=None, kind="consents")
    log.add("delete", f"{CON}/consents/urn:x", 423, T0, method="DELETE", obj=None, kind="consents")
    log.add("read", f"{CON}/consents/urn:x", 404, T0, obj=None, kind="consents")
    out = judged(spark, log)
    assert verdicts(out["create"]) == ["limit_not_applicable"] * 2
    assert verdicts(out["delete"]) == ["limit_not_applicable"]
    assert [r["verdict"] for r in out["read"]] == [None]


def small_run() -> Log:
    """A run with each kind of case: clean ones at their limits and one per limit class. The
    TPM 1000 endpoint keeps the traffic case small."""
    log = Log()
    detail = f"{ACC}/accounts/ACC-1"
    for case, defect, refused_at in (("traffic-clean", None, 1000), ("traffic-early", "early-traffic-limit/random", 500)):
        for i in range(refused_at):
            log.add(case, detail, 200 if i < 8 else 423, T0 + i * 10, kind="traffic")
        log.add(case, detail, 429, T0 + refused_at * 10, kind="traffic", defect=defect)
    for case, defect, successes in (("monthly-clean", None, 8), ("monthly-early", "early-operational-limit/off-by-one", 7)):
        for i in range(successes):
            log.add(case, detail, 200, T0 + i * 10)
        log.add(case, detail, 423, T0 + 100, defect=defect)
    log.add("consents-clean", f"{CON}/consents", 201, T0, method="POST", obj=None, kind="consents")
    log.add("consents-refused", f"{CON}/consents", 429, T0, method="POST", obj=None, kind="consents",
            defect="limit-not-applicable/429")
    return log


def test_report_holds_its_gate(spark, tmp_path, capsys):
    source = small_run().write(tmp_path)
    assert limits_job.main(["--run-id", "t", "--source", source, "--requirements", str(REQUIREMENTS), "--no-tables",
                            "--report", "t", "--gate"], spark=spark) == 0
    out = capsys.readouterr().out
    summary = json.loads(out.split("LIMITS_REPORT ", 1)[1])
    assert summary["gate"] == "ok" and summary["verdicts"] == {v: [1.0, 1.0] for v in judge.VERDICTS}
    assert "| `traffic_limit_early` | `tpm-at-limit` | not flagged | 1 | 1 |" in out
    assert "| `operational_limit_early` | `monthly-at-limit` | not flagged | 2 | 2 |" in out


def test_gate_notices_a_missed_flag_a_false_flag_and_an_edge(spark):
    from detection import gate, summarize

    log = small_run()
    rows = [r.asDict() for r in judge.units(judge.judge(spark.createDataFrame(log.rows, judge.CLIENT), REQ),
                                            spark.createDataFrame(log.labels)).collect()]
    assert gate(summarize(rows, judge.VERDICTS)) == []

    def broken(unit: str, verdict: str) -> list[str]:
        changed = [{**r, "flagged": not r["flagged"]} if r["unit"].startswith(unit) and r["verdict"] == verdict
                   else r for r in rows]
        return gate(summarize(changed, judge.VERDICTS))

    assert broken("traffic-early", "traffic_limit_early") == ["traffic_limit_early: recall 0/1"]
    assert broken("consents-clean", "limit_not_applicable") == ["limit_not_applicable: precision 1/2"]
    assert broken("monthly-clean", "operational_limit_early") == [
        "operational_limit_early: precision 1/2", "operational_limit_early at edge:monthly-at-limit: 1/2 as expected"]


def test_unmatched_requests_fail_the_job(spark, tmp_path, capsys):
    log = Log()
    log.add("x", "/open-banking/unknown/v1/thing", 200, T0)
    assert limits_job.main(["--run-id", "t", "--source", log.write(tmp_path), "--requirements", str(REQUIREMENTS),
                            "--no-tables"], spark=spark) == 1
    assert "1 requests match no endpoint" in capsys.readouterr().err


@pytest.mark.parametrize("path, endpoint", [
    (f"{ACC}/accounts?page=2&page-size=25", "GET /accounts"),
    (f"{ACC}/accounts/ACC-1/transactions-current?page=3", "GET /accounts/{accountId}/transactions-current"),
    (f"{CON}/consents/urn:a:b/extensions", "GET /consents/{consentId}/extensions"),
])
def test_endpoint_from_method_and_path(spark, path, endpoint):
    log = Log()
    log.add("x", path, 200, T0)
    assert judged(spark, log)["x"][0]["endpoint"] == endpoint
