"""The client's log of the limits load (infra/k6/limits.js) into the object store:
python -m contract_validator.limits_log <k6 log file>.

k6 writes one console record per request into a JSON log (--log-format=json); this keeps those
records, adds the run id (the limits proxy's, so the log and its labels share run=<id>) and writes
them as JSONL parts to s3://<bucket>/limits/client/run=<run_id>/. Prints a summary line; exits 1
when the log holds no request.
"""

from __future__ import annotations

import collections
import json
import os
import sys
from collections.abc import Iterable, Iterator
from typing import Any

import httpx2

from .results import PartWriter, S3Store, Store

FIELDS = ("case", "kind", "route", "origin", "object", "page", "method", "path", "interaction", "status",
          "start", "end")


def records(lines: Iterable[str]) -> Iterator[dict[str, Any]]:
    """The request records of a k6 JSON log; other log lines (k6's own) are left out."""
    for line in lines:
        try:
            entry = json.loads(line)
            record = json.loads(entry["msg"]) if entry.get("source") == "console" else None
        except (ValueError, KeyError, TypeError):
            continue
        if isinstance(record, dict) and set(FIELDS) <= set(record):
            yield record


def upload(lines: Iterable[str], store: Store, run_id: str) -> dict[str, Any]:
    writer = PartWriter(store, f"limits/client/run={run_id}/", "k6", part_size=5000)
    kinds: collections.Counter = collections.Counter()
    statuses: collections.Counter = collections.Counter()
    for record in records(lines):
        writer.add({"run_id": run_id, **record})
        kinds[record["kind"]] += 1
        statuses[record["status"]] += 1
    writer.flush()
    return {"run_id": run_id, "requests": writer.written, "prefix": writer.prefix,
            "by_kind": dict(sorted(kinds.items())), "by_status": {str(k): v for k, v in sorted(statuses.items())}}


def main() -> None:
    env = os.environ
    store = S3Store(
        bucket=env.get("RESULTS_BUCKET", "lakehouse"), endpoint=env["S3_ENDPOINT"],
        access_key=env["S3_ACCESS_KEY_ID"], secret_key=env["S3_SECRET_ACCESS_KEY"],
        region=env.get("AWS_REGION", "us-east-1"),
    )
    target = env.get("TARGET", "http://proxy-limits.ofb.svc")
    with httpx2.Client(base_url=target, timeout=15.0) as client:
        run_id = client.get("/_proxy/run").json()["run_id"]
        with open(sys.argv[1], encoding="utf-8") as f:
            summary = upload(f, store, run_id)
        # The proxy's labels of the same run, written now.
        client.post("/_proxy/flush")
    print("LIMITS", json.dumps(summary), flush=True)
    if not summary["requests"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
