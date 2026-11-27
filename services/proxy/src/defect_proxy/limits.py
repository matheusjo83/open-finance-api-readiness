"""Limit defects (operational layer): a transmitter that refuses too early, or where no limit
applies (IN 615 5.1.1 and 5.2, docs/defects.md).

They are drawn per test case, not per response: a test case is one origin (the caller's
`x-mock-client-org-id`) on one endpoint, and the load tool repeats the same request many times,
so a draw per request would be all or nothing. One uniform draw from the seed, the origin and the
route walks the applicable classes, as the response draw does. A drawn case keeps counters, and
its responses are refused from the request the variant names on:

- early-traffic-limit: the case's limit of the minute is below the TPM: `random`, the k-th
  request of a minute (k drawn below the TPM) and the rest of that minute get 429; `off-by-one`,
  from the TPM-th request (it must be the one after it).
- early-operational-limit: the monthly limit is reached too early, so successes of first pages
  become 423 from: `random`, the k-th success (k below the minimum); `off-by-one`, the
  minimum-th; `counts-pagination`, when the 2XX counted with the pages after the first reach the
  minimum; `counts-errors`, when the first pages counted whatever their status reach it.
- limit-not-applicable: a Consents endpoint refused, from the k-th request of a minute (k from 1
  to 20), with 429 or 423.

A `counts-*` case changes nothing unless the traffic paginates or fails before the transmitter's
own limit: then no exchange carries the class, and the case is not a positive.
"""

from __future__ import annotations

import hashlib
import json
import random
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone

from .catalogue import Exchange, Injection

ORIGIN_HEADER = "x-mock-client-org-id"
DEFAULT_ORIGIN = str(uuid.uuid5(uuid.NAMESPACE_DNS, "receiver.ofb.svc"))
JSON_ERROR = "application/json; charset=utf-8"

# Per route: (TPM, calls per month), the minimums of specs/operational/requirements.json (a test
# checks they agree). None: the limit does not apply.
MINIMUMS: dict[str, tuple[int | None, int | None]] = {
    "consents.create": (None, None),
    "consents.read": (None, None),
    "consents.delete": (None, None),
    "consents.extend": (None, None),
    "consents.extensions": (None, None),
    "accounts.list": (1000, 8),
    "accounts.detail": (1000, 8),
    "accounts.balances": (2500, 420),
    "accounts.transactions": (1000, 8),
    "accounts.transactions-current": (2500, 240),
    "accounts.overdraft-limits": (2500, 420),
}
NOT_APPLICABLE_MAX_K = 20


@dataclass(frozen=True)
class LimitDefect:
    id: str
    variants: tuple[str, ...]
    layer: str = "operational"
    default_rate: float = 0.0

    def applies(self, route: str | None) -> bool:
        if route not in MINIMUMS:
            return False
        tpm, monthly = MINIMUMS[route]
        if self.id == "early-traffic-limit":
            return tpm is not None
        if self.id == "early-operational-limit":
            return monthly is not None
        return tpm is None and monthly is None


# Off by default: a proxy of their own turns them on (infra/k8s/proxy-limits.yaml).
LIMIT_CATALOGUE = [
    LimitDefect("early-traffic-limit", ("random", "off-by-one")),
    LimitDefect("early-operational-limit", ("random", "off-by-one", "counts-pagination", "counts-errors")),
    LimitDefect("limit-not-applicable", ("429", "423")),
]
BY_ID = {d.id: d for d in LIMIT_CATALOGUE}


def _uniform(text: str) -> float:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big") / 2**64


@dataclass
class Case:
    defect: LimitDefect
    variant: str
    k: int  # the threshold of random, off-by-one and limit-not-applicable
    minutes: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    successes: int = 0  # upstream 2XX of first pages
    with_pages: int = 0  # upstream 2XX, pages after the first included
    first_pages: int = 0  # first pages, whatever their status
    refused: int = 0
    exhausted: bool = False


def draw(seed: int, rates: dict[str, float], origin: str, route: str | None) -> Case | None:
    key = f"{seed}|limits|{origin}|{route}"
    u, cumulative = _uniform(key), 0.0
    for defect in LIMIT_CATALOGUE:
        rate = rates.get(defect.id, 0.0)
        if rate <= 0 or not defect.applies(route):
            continue
        cumulative += rate
        if u < cumulative:
            rng = random.Random(_uniform(f"{key}|{defect.id}"))
            variant = rng.choice(defect.variants)
            tpm, monthly = MINIMUMS[route]
            if defect.id == "early-traffic-limit":
                k = tpm if variant == "off-by-one" else rng.randint(1, tpm - 1)
            elif defect.id == "early-operational-limit":
                k = monthly if variant == "off-by-one" else rng.randint(1, monthly - 1)
            else:
                k = rng.randint(1, NOT_APPLICABLE_MAX_K)
            return Case(defect, variant, k)
    return None


def follow_up_page(ex: Exchange) -> bool:
    page = ex.query_value("page") or "1"
    return page.isdigit() and int(page) > 1


def refuse(ex: Exchange, status: int) -> None:
    """The response becomes a limit refusal, in the error envelope the transmitter uses."""
    title, code = {429: ("Limite de tráfego excedido", "LIMITE_TRAFEGO_EXCEDIDO"),
                   423: ("Limite operacional excedido", "LIMITE_OPERACIONAL_EXCEDIDO")}[status]
    meta = {"requestDateTime": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
    if ex.route and ex.route.startswith("accounts."):
        meta = {"totalRecords": 1, "totalPages": 1, **meta}
    ex.status = status
    ex.body = {"errors": [{"code": code, "title": title, "detail": title + "."}], "meta": meta}
    ex.headers = {k: v for k, v in ex.headers.items() if k != "content-type"} | {"content-type": JSON_ERROR}


class LimitDefects:
    """The cases drawn so far and their counters (one proxy replica)."""

    def __init__(self, seed: int, rates: dict[str, float]) -> None:
        self.seed, self.rates = seed, rates
        self.enabled = any(rates.get(d.id, 0) > 0 for d in LIMIT_CATALOGUE)
        self.cases: dict[tuple[str, str | None], Case | None] = {}

    def apply(self, ex: Exchange, origin: str, now: datetime) -> tuple[LimitDefect, Injection] | None:
        if not self.enabled or ex.route not in MINIMUMS:
            return None
        key = (origin, ex.route)
        if key not in self.cases:
            self.cases[key] = draw(self.seed, self.rates, origin, ex.route)
        case = self.cases[key]
        if case is None:
            return None
        if case.defect.id == "early-operational-limit":
            return self._operational(case, ex)
        minute = now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        case.minutes[minute] += 1
        n = case.minutes[minute]
        status = 429 if case.defect.id == "early-traffic-limit" else int(case.variant)
        if n < case.k or ex.status == status:
            return None
        before = ex.status
        refuse(ex, status)
        return case.defect, Injection(case.variant, "status", json.dumps(
            {"from": before, "to": status, "request_of_minute": n, "k": case.k}))

    def _operational(self, case: Case, ex: Exchange) -> tuple[LimitDefect, Injection] | None:
        upstream_ok = 200 <= ex.upstream_status < 300
        if follow_up_page(ex):
            case.with_pages += upstream_ok
            return None
        counted = {"random": case.successes, "off-by-one": case.successes,
                   "counts-pagination": case.with_pages, "counts-errors": case.first_pages}[case.variant]
        threshold = case.k - 1 if case.variant in ("random", "off-by-one") else MINIMUMS[ex.route][1]
        case.first_pages += 1
        if upstream_ok:
            case.successes += 1
            case.with_pages += 1
        if not upstream_ok or not (case.exhausted or counted >= threshold):
            return None
        case.exhausted = True
        served = case.successes - 1 - case.refused
        case.refused += 1
        before = ex.status
        refuse(ex, 423)
        return case.defect, Injection(case.variant, "status", json.dumps(
            {"from": before, "to": 423, "counted_before": counted, "served_before": served}))
