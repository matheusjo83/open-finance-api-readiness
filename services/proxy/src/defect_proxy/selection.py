"""Which defect, if any, an exchange gets: a deterministic function of the seed and the request.

The key is the seed, the method, the path, the sorted query, the consent (of the bearer token:
caller.py) and, from the second time the same request comes in the proxy's run, its occurrence;
the interaction id and the token itself are left out (a JWT is new on every login), so a walk
draws the same in every run. The occurrence keeps a repeated request a draw of its own, as the
walk's second read of a consent (decision 33). One uniform draw from
the key walks the applicable classes, in catalogue order, accumulating their rates: at most one
defect per response, and each class is chosen with probability equal to its rate.
"""

from __future__ import annotations

import hashlib
import json
import random

from .catalogue import CATALOGUE, Defect, Exchange
from .limits import LIMIT_CATALOGUE


def parse_rates(raw: str | None) -> dict[str, float]:
    """Per-class rates: the defaults, overridden by a JSON object (DEFECT_RATES). The response
    classes and the limit classes are two draws, so each group adds up to at most 1."""
    rates = {d.id: d.default_rate for d in CATALOGUE} | {d.id: d.default_rate for d in LIMIT_CATALOGUE}
    if raw:
        override = json.loads(raw)
        unknown = set(override) - set(rates)
        if unknown:
            raise ValueError(f"unknown defect classes in DEFECT_RATES: {sorted(unknown)}")
        rates.update({k: float(v) for k, v in override.items()})
    if any(not 0 <= r <= 1 for r in rates.values()):
        raise ValueError("defect rates must be between 0 and 1")
    for group, name in ((CATALOGUE, "response"), (LIMIT_CATALOGUE, "limit")):
        total = sum(rates[d.id] for d in group)
        if total > 1:
            raise ValueError(f"{name} defect rates add up to {total:.2f}; at most 1 so one draw picks one class")
    return rates


def request_key(seed: int, ex: Exchange, occurrence: int = 1) -> str:
    query = "&".join(f"{k}={v}" for k, v in sorted(ex.query))
    key = f"{seed}|{ex.method}|{ex.path}|{query}|{ex.consent or ''}"
    return key if occurrence == 1 else f"{key}|{occurrence}"


def _uniform(text: str) -> float:
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big") / 2**64


def choose(seed: int, rates: dict[str, float], ex: Exchange,
           occurrence: int = 1) -> tuple[Defect, random.Random] | None:
    key = request_key(seed, ex, occurrence)
    draw, cumulative = _uniform(key), 0.0
    for defect in CATALOGUE:
        rate = rates.get(defect.id, 0.0)
        if rate <= 0 or not defect.applies(ex):
            continue
        cumulative += rate
        if draw < cumulative:
            return defect, random.Random(_uniform(f"{key}|{defect.id}"))
    return None
