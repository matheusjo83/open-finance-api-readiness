"""The history generator (docs/history.md): every unit reaches what its episode was built for,
the reports say what the ledger says (recounted from the reports alone, as the PCM would pair
them), the generation is deterministic, and the Spark job writes reports and labels."""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

JOBS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(JOBS / "history"))

from zoneinfo import ZoneInfo  # noqa: E402

from generator import (CHRONIC_DAYS, Settings, chronic_days, chronic_minutes, endpoints,  # noqa: E402
                       episode, unit)

REQUIREMENTS = json.loads((JOBS.parent / "specs/operational/requirements.json").read_text())
EPS = endpoints(REQUIREMENTS)
# The time zone the requirements name, not the generator's fixed offset.
BRT = ZoneInfo(REQUIREMENTS["availability"]["timezone"])


def recount(client: list[dict], server: list[dict], day: date) -> dict:
    """The unit's metrics from its reports alone: pairing by fapiInteractionId on the five fields,
    the reports sent by 08:00 of the next day and within 7 days, the client's view with the
    server's when unpaired (docs/operational.md)."""
    fields = ("clientOrgId", "serverOrgId", "statusCode", "httpMethod", "endpoint")
    c = {r["report"]["fapiInteractionId"]: r for r in client}
    s = {r["report"]["fapiInteractionId"]: r for r in server}
    cut = (datetime.combine(day, datetime.min.time(), tzinfo=BRT) + timedelta(days=1, hours=8)).astimezone(timezone.utc)
    sent = lambda r: datetime.fromisoformat(r["sent_at"].replace("Z", "+00:00"))  # noqa: E731
    calls = set(c) | set(s)
    out = {"calls": len(calls), "unpaired_8h": 0, "unpaired_7d": 0, "inconsistent": 0, "valid": 0, "n529": 0}
    times, minutes = [], defaultdict(lambda: [0, 0])
    for i in calls:
        if i in c and i in s:
            if any(c[i]["report"][f] != s[i]["report"][f] for f in fields):
                out["inconsistent"] += 1
                continue
            out["unpaired_8h"] += sent(c[i]) > cut or sent(s[i]) > cut
            report = c[i]["report"]
        else:
            out["unpaired_7d"] += 1
            out["unpaired_8h"] += 1
            report = (c.get(i) or s[i])["report"]
        status = report["statusCode"]
        if status not in (423, 429, 529) and report["processTimespan"] > 0:
            times.append(report["processTimespan"])
        ok = 200 <= status < 300 or status == 422
        error = 500 <= status < 600 or status == 408
        if ok or error:
            out["valid"] += 1
            out["n529"] += status == 529
            local = datetime.fromisoformat(report["timestamp"].replace("Z", "+00:00")).astimezone(BRT)
            assert local.date() == day
            minutes[local.hour * 60 + local.minute][error] += 1
    times.sort()
    out["n_rt"] = len(times)
    out["p95_ms"] = times[max(1, math.floor(0.95 * len(times) + 0.5)) - 1] if times else None
    out["minutes_available"] = sum(1 for ok, err in minutes.values() if ok / (ok + err) >= 0.95)
    out["minutes_unavailable"] = len(minutes) - out["minutes_available"]
    return out


@pytest.mark.parametrize("seed", [1, 2, 3])
def test_every_unit_meets_its_episode(seed):
    """unit() raises when an episode misses its threshold; the reports recount to the ledger."""
    s = Settings(seed=seed)
    seen = set()
    for ep in EPS:
        chronic = chronic_days(s, ep)
        for day in s.day_list():
            client, server, ledger = unit(s, ep, day, day in chronic, EPS)
            seen.add(ledger["class"] or ledger["edge"])
            assert {k: ledger[k] for k in recount(client, server, day)} == recount(client, server, day), (ep.key, day)
    assert seen >= {None, "overload", "unpaired", "inconsistent-pair", "slow-day", "outage"}


def test_every_class_and_edge_appears_across_seeds():
    seen = set()
    for seed in range(1, 6):
        s = Settings(seed=seed)
        for ep in EPS:
            for day in s.day_list():
                _, cls, variant = episode(s, ep, day)
                if cls:
                    seen.add(variant if cls == "edge" else f"{cls}/{variant}")
    assert seen >= {"overload/over-0.5", "overload/over-5", "overload-at-0.5", "p95-at-sla", "minute-at-95",
                    "day-at-95", "slow-day/mild", "slow-day/severe", "outage/daily", "inconsistent-pair/status",
                    "inconsistent-pair/endpoint", "unpaired/late-client", "unpaired/late-server",
                    "unpaired/missing-some-client", "unpaired/missing-some-server",
                    "unpaired/missing-many-client", "unpaired/missing-many-server"}


def test_chronic_endpoint_loses_the_long_availability():
    s = Settings(seed=1, rates={"chronic-endpoints": 1.0})
    ep = EPS[0]
    days = chronic_days(s, ep)
    assert len(days) == CHRONIC_DAYS and all(episode(s, ep, d)[1] is None for d in days)
    daily = []
    for day in s.day_list():
        _, _, ledger = unit(s, ep, day, day in days, EPS)
        defined = ledger["minutes_available"] + ledger["minutes_unavailable"]
        daily.append(ledger["minutes_available"] / defined)
    assert min(daily) >= 0.95 and sum(daily) / len(daily) < 0.995
    assert chronic_minutes(Settings(minutes=1440)) <= 0.05 * 1440
    with pytest.raises(ValueError):
        chronic_minutes(Settings(minutes=10))


def test_deterministic_per_unit():
    s, ep, day = Settings(seed=7), EPS[3], date(2026, 10, 1)
    assert unit(s, ep, day, False, EPS) == unit(s, ep, day, False, EPS)
    assert unit(s, ep, day, False, EPS)[0] != unit(Settings(seed=8), ep, day, False, EPS)[0]
    # A unit does not depend on the run's length: the same day of a longer history.
    longer = Settings(seed=7, days=120)
    assert unit(s, ep, day, False, EPS)[0] == [{**r, "run_id": s.run_id} for r in unit(longer, ep, day, False, EPS)[0]]


def test_reports_have_the_live_format():
    """The fields of the live telemetry (docs/telemetry.md), on both sides."""
    s = Settings(seed=1)
    client, server, _ = unit(s, EPS[0], s.end, False, EPS)
    assert set(client[0]) == {"run_id", "walk_id", "sent_at", "report"} and set(server[0]) == {"run_id", "sent_at", "report"}
    assert set(client[0]["report"]) == {"role", "endpoint", "httpMethod", "statusCode", "clientOrgId", "serverOrgId",
                                       "fapiInteractionId", "timestamp", "processTimespan", "clientSSId",
                                       "endpointUriPrefix", "additionalInfo"}
    assert set(server[0]["report"]) == {"role", "endpoint", "httpMethod", "statusCode", "clientOrgId", "serverOrgId",
                                       "fapiInteractionId", "timestamp", "processTimespan", "additionalInfo"}


def test_job_writes_reports_and_labels(spark, tmp_path):
    from job import main  # noqa: E402

    # The executors' Python gets the module as in the cluster (deps.pyFiles).
    spark.sparkContext.addPyFile(str(JOBS / "history" / "generator.py"))
    assert main(["--seed", "2", "--minutes", "20", "--per-minute", "1", "--source", f"file://{tmp_path}",
                 "--requirements", str(JOBS.parent / "specs/operational/requirements.json"), "--partitions", "4"],
                spark=spark) == 0
    run = "run=history-s2-90d-20x1"
    client = spark.read.json(f"{tmp_path}/pcm/client/{run}/")
    server = spark.read.json(f"{tmp_path}/pcm/server/{run}/")
    labels = {scope: [r.asDict() for r in spark.read.json(f"{tmp_path}/pcm-labels/{run}/{scope}/").collect()]
              for scope in ("endpoint-day", "endpoint", "api-day", "institution-day")}
    assert len(labels["endpoint-day"]) == 90 * len(EPS) and len(labels["endpoint"]) == len(EPS)
    assert len(labels["api-day"]) == 180 and len(labels["institution-day"]) == 90
    calls = sum(r["calls"] for r in labels["endpoint-day"])
    unpaired = sum(r["unpaired_7d"] for r in labels["endpoint-day"])
    assert client.count() + server.count() == 2 * calls - unpaired

    for r in labels["endpoint-day"]:
        if r["class"] == "slow-day" or r["edge"] == "p95-at-sla":
            assert r["rt_ok"] == (r["edge"] == "p95-at-sla")
        if r["class"] == "outage":
            assert r["availability_ok"] is False
    for r in labels["institution-day"]:
        assert r["overload_ok"] == (r["class"] is None and r["edge"] is None)
        assert r["overload_over_5"] == (r["variant"] == "over-5")
    for r in labels["api-day"]:
        variant = r["variant"] or ""
        assert r["reporting_8h_ok"] == (r["class"] != "unpaired" or variant.startswith("missing-some"))
        assert r["reporting_7d_ok"] == (r["class"] is None or variant.startswith("late"))
    chronic = {f"{r['method']} {r['endpoint']}" for r in labels["endpoint"] if r["chronic"]}
    assert chronic == {ep.key for ep in EPS if chronic_days(Settings(seed=2), ep)}
    assert all(not r["long_ok"] for r in labels["endpoint"] if r["chronic"])


def test_same_institutions_as_the_live_telemetry():
    """The history's reports name the institutions the validator and the proxy name, so the
    operational job reads both kinds of run alike."""
    import ast
    import uuid

    from generator import CLIENT_ORG, CLIENT_SS, SERVER_ORG, URI_PREFIX

    for module in ("services/validator/src/contract_validator/telemetry.py", "services/proxy/src/defect_proxy/telemetry.py"):
        tree = ast.parse((JOBS.parent / module).read_text())
        names = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        assert {"receiver.ofb.svc", "transmitter.ofb.svc"} <= names, module
    assert (CLIENT_ORG, SERVER_ORG) == (str(uuid.uuid5(uuid.NAMESPACE_DNS, "receiver.ofb.svc")),
                                        str(uuid.uuid5(uuid.NAMESPACE_DNS, "transmitter.ofb.svc")))
    validator = (JOBS.parent / "services/validator/src/contract_validator/telemetry.py").read_text()
    assert '"software-statement.receiver.ofb.svc"' in validator and f'"{URI_PREFIX}"' in validator
    assert CLIENT_SS == str(uuid.uuid5(uuid.NAMESPACE_DNS, "software-statement.receiver.ofb.svc"))
