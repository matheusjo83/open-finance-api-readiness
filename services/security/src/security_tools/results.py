"""The outcome of a conformance plan, from the suite's API: one record per test module, with the
conditions that failed or warned and the requirements they cite. This, not the runner's exit
code, is what the security layer judges (stage 4 turns it into a detection report)."""

import re
from collections import Counter

import httpx

PLAN_ID = re.compile(r"Created test plan, new id: (\w+)")


def plan_id(runner_output: str) -> str | None:
    found = PLAN_ID.findall(runner_output)
    return found[-1] if found else None


def condition(entry: dict) -> dict:
    return {"condition": entry.get("src"), "result": entry.get("result"),
            "requirements": entry.get("requirements") or [], "message": str(entry.get("msg", ""))[:500]}


def collect(api_base: str, plan: str, *, verify=False) -> list[dict]:
    """Every module of the plan, in plan order, with its last instance's outcome."""
    with httpx.Client(base_url=api_base.rstrip("/") + "/api/", verify=verify, timeout=60) as client:
        detail = client.get(f"plan/{plan}").raise_for_status().json()
        records = []
        for module in detail.get("modules", []):
            instances = module.get("instances") or []
            record = {"module": module.get("testModule"), "variant": module.get("variant") or {},
                      "instance": instances[-1] if instances else None, "status": None, "result": None,
                      "failures": [], "warnings": []}
            if record["instance"]:
                info = client.get(f"info/{record['instance']}").raise_for_status().json()
                record["status"], record["result"] = info.get("status"), info.get("result")
                for entry in client.get(f"log/{record['instance']}").raise_for_status().json():
                    if entry.get("result") == "FAILURE":
                        record["failures"].append(condition(entry))
                    elif entry.get("result") == "WARNING":
                        record["warnings"].append(condition(entry))
            records.append(record)
        return records


def outcome(record: dict, known: dict) -> str:
    """passed, skipped, review (the suite waits for a human to look at evidence), known (only
    the expected conditions failed, as KNOWN says) or unexpected."""
    if record["status"] == "FINISHED" and record["result"] == "PASSED":
        return "passed"
    if record["status"] == "FINISHED" and record["result"] == "SKIPPED":
        return "skipped"
    if record["status"] == "FINISHED" and record["result"] == "REVIEW" and not record["failures"]:
        return "review"
    conditions = {f["condition"] for f in record["failures"]}
    if record["result"] == "WARNING":
        conditions |= {w["condition"] for w in record["warnings"]}
    expected = known.get(record["module"], (set(), ""))[0]
    return "known" if conditions and conditions <= expected else "unexpected"


def judge(records: list[dict], known: dict) -> list[dict]:
    return [{**r, "outcome": outcome(r, known)} for r in records]


def summary(records: list[dict]) -> dict:
    return {"modules": len(records),
            "by_result": dict(Counter(f"{r['status']}/{r['result']}" for r in records)),
            "by_outcome": dict(Counter(r.get("outcome") for r in records)),
            "unexpected": sorted(r["module"] for r in records if r.get("outcome") == "unexpected"),
            "review": sorted(r["module"] for r in records if r.get("outcome") == "review")}
