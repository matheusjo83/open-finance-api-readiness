"""The client's log of the limits load: only the request records of the k6 log, with the run id."""

import json

from contract_validator.limits_log import records, upload
from contract_validator.results import MemoryStore

RECORD = {"case": "traffic-0", "kind": "traffic", "route": "accounts.list", "origin": "o", "object": "c",
          "page": 1, "method": "GET", "path": "/open-banking/accounts/v2/accounts", "interaction": "i",
          "status": 200, "start": 1, "end": 2}


def line(msg: str, source: str = "console") -> str:
    return json.dumps({"level": "info", "msg": msg, "source": source, "time": "2026-11-27T01:30:02Z"})


def test_only_request_records_are_kept():
    lines = [line(json.dumps(RECORD)), line("k6 starting", source=""), "not json",
             line(json.dumps({"case": "x"})), line(json.dumps({**RECORD, "status": 429}))]
    assert [r["status"] for r in records(lines)] == [200, 429]


def test_upload_adds_the_run_id():
    store = MemoryStore()
    summary = upload([line(json.dumps(RECORD))] * 3, store, "limits-s42")
    assert summary["requests"] == 3 and summary["by_kind"] == {"traffic": 3} and summary["by_status"] == {"200": 3}
    assert store.records("limits/client/run=limits-s42/") == [{"run_id": "limits-s42", **RECORD}] * 3
