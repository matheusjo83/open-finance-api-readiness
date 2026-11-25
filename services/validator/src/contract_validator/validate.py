"""One validation run: walk the target, check every Open Finance exchange, write the results,
the exchanges and the client's PCM reports, and return a summary."""

from __future__ import annotations

import collections
import uuid
from datetime import datetime, timezone
from typing import Any

from .results import PartWriter, Store
from .rules import RULES, Exchange, check
from .spec import Spec, spec_for
from .telemetry import Parties, client_record
from .walk import HttpClient, Walker


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def proxy_run_id(client: HttpClient) -> str | None:
    """The proxy's run id, so results and labels share s3://.../run=<id>/; None without a proxy."""
    try:
        response = client.request("GET", "/_proxy/run", headers={})
    except Exception:
        return None
    if response.status_code != 200 or "json" not in response.headers.get("content-type", ""):
        return None
    return response.json().get("run_id")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def result_record(run_id: str, walk_id: str, seq: int, spec: Spec, ex: Exchange) -> dict[str, Any]:
    op, findings = check(spec, ex)
    rules = sorted({f.rule for f in findings})
    severities = {f.severity for f in findings}
    return {
        "run_id": run_id, "walk_id": walk_id, "seq": seq, "ts": _now(), "interaction_id": ex.interaction_id,
        "method": ex.method, "path": ex.path, "query": ex.query or None,
        "api": spec.name, "operation": op.id if op else None, "status": ex.status,
        "verdict": "fail" if "error" in severities else "warn" if severities else "pass",
        "rules": rules, "findings": [f.as_dict() for f in findings],
    }


def exchange_record(run_id: str, walk_id: str, seq: int, ex: Exchange, operation: str | None) -> dict[str, Any]:
    return {
        "run_id": run_id, "walk_id": walk_id, "seq": seq, "ts": _now(), "interaction_id": ex.interaction_id,
        "method": ex.method, "path": ex.path, "query": ex.query or None, "operation": operation,
        "request_headers": {k: v for k, v in ex.request_headers.items() if k != "authorization"},
        "consent": ex.request_headers.get("authorization", "")[7:] or None,
        "request_body": ex.request_body.decode() if ex.request_body else None,
        "status": ex.status, "response_headers": ex.headers,
        "response_body": ex.body.decode("utf-8", errors="replace") if ex.body else None,
    }


def run(client: HttpClient, specs: list[Spec], store: Store, run_id: str | None = None,
        walker: Walker | None = None, parties: Parties = Parties()) -> dict[str, Any]:
    run_id = proxy_run_id(client) or run_id or new_run_id()
    walk_id = uuid.uuid4().hex[:8]
    results = PartWriter(store, f"contract/run={run_id}/", walk_id)
    exchanges = PartWriter(store, f"exchanges/run={run_id}/", walk_id)
    reports = PartWriter(store, f"pcm/client/run={run_id}/", walk_id)
    verdicts, by_rule = collections.Counter(), collections.Counter()
    seq = 0
    for ex in (walker or Walker(client)).walk():
        spec = spec_for(specs, ex.path)
        if spec is None:
            continue
        seq += 1
        record = result_record(run_id, walk_id, seq, spec, ex)
        results.add(record)
        exchanges.add(exchange_record(run_id, walk_id, seq, ex, record["operation"]))
        telemetry = client_record(run_id, walk_id, spec, ex, parties)
        if telemetry is not None:
            reports.add(telemetry)
        verdicts[record["verdict"]] += 1
        by_rule.update(record["rules"])
    results.flush()
    exchanges.flush()
    reports.flush()
    return {
        "run_id": run_id, "walk_id": walk_id, "exchanges": results.written,
        "results": f"contract/run={run_id}/", "exchange_log": f"exchanges/run={run_id}/",
        "pcm_reports": reports.written, "telemetry": f"pcm/client/run={run_id}/",
        "verdicts": dict(sorted(verdicts.items())),
        "by_rule": {rule: by_rule[rule] for rule in RULES if by_rule[rule]},
    }
