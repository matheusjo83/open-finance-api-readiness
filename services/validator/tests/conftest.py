"""Fixtures: the real transmitter in process, the defect proxy in front of it (labels in memory)
and the validator walking through the proxy, its results in memory too."""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import httpx2
import pytest
from starlette.testclient import TestClient

from contract_validator.results import MemoryStore
from contract_validator.spec import load_specs
from contract_validator.validate import run
from contract_validator.walk import Walker
from defect_proxy.app import create_app as create_proxy
from defect_proxy.catalogue import CATALOGUE
from defect_proxy.labels import LabelSink
from defect_proxy.labels import MemoryStore as LabelStore
from transmitter.app import create_app as create_transmitter

REPO = Path(__file__).resolve().parents[3]
SPECS = Path(os.environ.get("SPECS_DIR", REPO / "specs" / "upstream"))
NOW = datetime(2026, 11, 16, 12, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(scope="session")
def specs():
    try:
        return load_specs(SPECS)
    except FileNotFoundError as e:
        pytest.exit(str(e), returncode=2)


def zero_rates() -> dict[str, float]:
    return {d.id: 0.0 for d in CATALOGUE}


def only(defect_id: str) -> dict[str, float]:
    return {**zero_rates(), defect_id: 1.0}


class Run:
    """A validation run through the proxy: the summary, the results and the proxy's labels."""

    def __init__(self, summary: dict, results: list[dict], exchanges: list[dict], labels: list[dict]) -> None:
        self.summary, self.results, self.exchanges, self.labels = summary, results, exchanges, labels
        self.label_of = {r["interaction_id"]: r for r in labels}

    def joined(self) -> list[tuple[dict, dict]]:
        """(result, label) for every result, by the interaction id the validator sent."""
        return [(r, self.label_of[r["interaction_id"]]) for r in self.results]


@pytest.fixture
def validate(specs):
    """validate(rates, seed=42) -> Run, over a fresh transmitter and proxy."""
    opened = []

    def make(rates: dict[str, float], seed: int = 42) -> Run:
        transmitter = create_transmitter(seed=42, customers=5, clock=lambda: NOW)
        labels = LabelStore()
        proxy = create_proxy(httpx2.ASGITransport(app=transmitter), seed=seed, rates=rates,
                             sink=LabelSink(labels, run_id="test-run"), flush_seconds=3600)
        client = TestClient(proxy)
        client.__enter__()
        opened.append(client)
        store = MemoryStore()
        summary = run(client, specs, store, walker=Walker(client, today=lambda: NOW.date()))
        client.post("/_proxy/flush")
        return Run(summary, store.records("contract/"), store.records("exchanges/"), labels.records())

    yield make
    for client in opened:
        client.__exit__(None, None, None)
