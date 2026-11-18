"""Exports validation runs for the business-rules job's tests (make jobs-test): for each
scenario, the validator walks the proxy over the real transmitter, in process, and the run is
written as it would be in the object store (exchanges/, contract/, labels/ under run=<id>/).

Usage: python -m tests.export_runs <directory>
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx2
from starlette.testclient import TestClient

from contract_validator.results import MemoryStore
from contract_validator.validate import run
from contract_validator.walk import Walker
from defect_proxy.app import create_app as create_proxy
from defect_proxy.labels import LabelSink
from defect_proxy.labels import MemoryStore as LabelStore
from defect_proxy.selection import parse_rates
from transmitter.app import create_app as create_transmitter

from .conftest import NOW, SPECS, load_specs, only, zero_rates

BUSINESS = ["inconsistent-balance", "broken-pagination", "consent-not-enforced", "transaction-outside-window"]
SCENARIOS = {"clean": zero_rates(), "default": parse_rates(None), **{d: only(d) for d in BUSINESS}}


def export(directory: Path, name: str, rates: dict[str, float], specs) -> dict:
    transmitter = create_transmitter(seed=42, customers=5, clock=lambda: NOW)
    labels = LabelStore()
    proxy = create_proxy(httpx2.ASGITransport(app=transmitter), seed=42, rates=rates,
                         sink=LabelSink(labels, run_id=name), flush_seconds=3600)
    store = MemoryStore()
    with TestClient(proxy) as client:
        summary = run(client, specs, store, walker=Walker(client, today=lambda: NOW.date()))
        client.post("/_proxy/flush")
    for key, body in {**store.objects, **labels.objects}.items():
        path = directory / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    return summary


def main() -> None:
    directory = Path(sys.argv[1])
    specs = load_specs(SPECS)
    meta = {"now": NOW.isoformat(), "scenarios": {}}
    for name, rates in SCENARIOS.items():
        meta["scenarios"][name] = export(directory, name, rates, specs)
    (directory / "runs.json").write_text(json.dumps(meta, indent=1))
    print(f"exported {len(SCENARIOS)} runs to {directory}")


if __name__ == "__main__":
    main()
