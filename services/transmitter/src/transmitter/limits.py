"""Traffic and monthly limits (Manual de APIs 7.0, IN BCB 615, 5.1.1 and 5.2).

The transmitter applies both at the minimums by default (docs/operational.md):
- traffic: per endpoint and origin (the caller's organisationId) in each calendar minute; the
  request past the limit gets 429. Consents have none.
- monthly: per month (Brasília time), endpoint, most granular object (the consent for the list of
  accounts, the account below it), customer (CPF) and receiver. Only 2XX count, and the pages after
  the first do not; the request past the limit gets 423, a page after the first never does.

A request refused with 429 is not counted towards the minute. Two requests of the same key that
pass the check together may both be served: a limit may let more through, never fewer.
"""

from __future__ import annotations

import json
import re
import threading
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

BRASILIA = timezone(timedelta(hours=-3))  # no daylight saving time since 2019

# Per endpoint: (TPM, calls per month), the minimums of the manual and the portal's table, as in
# specs/operational/requirements.json (a test checks they agree). None: the limit does not apply.
MINIMUMS: dict[str, tuple[int | None, int | None]] = {
    "POST /consents": (None, None),
    "GET /consents/{consentId}": (None, None),
    "DELETE /consents/{consentId}": (None, None),
    "POST /consents/{consentId}/extends": (None, None),
    "GET /consents/{consentId}/extensions": (None, None),
    "GET /accounts": (1000, 8),
    "GET /accounts/{accountId}": (1000, 8),
    "GET /accounts/{accountId}/balances": (2500, 420),
    "GET /accounts/{accountId}/transactions": (1000, 8),
    "GET /accounts/{accountId}/transactions-current": (2500, 240),
    "GET /accounts/{accountId}/overdraft-limits": (2500, 420),
}

# The alta TPM by the receiver's active consents with the transmitter (5.1.1 I): up to each count,
# then 2000 more per band of two million above six million.
ALTA_BANDS = ((1_000_000, 2500), (2_000_000, 5000), (3_000_000, 8000), (6_000_000, 10000))

CONSENTS = "/open-banking/consents/v3"
ACCOUNTS = "/open-banking/accounts/v2"
SEGMENT = r"[^/]+"
ROUTES = [
    (method, re.compile("^" + base + re.sub(r"\{\w+\}", SEGMENT, template) + "$"), f"{method} {template}")
    for base, method, template in [
        (CONSENTS, "POST", "/consents"),
        (CONSENTS, "GET", "/consents/{consentId}"),
        (CONSENTS, "DELETE", "/consents/{consentId}"),
        (CONSENTS, "POST", "/consents/{consentId}/extends"),
        (CONSENTS, "GET", "/consents/{consentId}/extensions"),
        (ACCOUNTS, "GET", "/accounts"),
        (ACCOUNTS, "GET", "/accounts/{accountId}"),
        (ACCOUNTS, "GET", "/accounts/{accountId}/balances"),
        (ACCOUNTS, "GET", "/accounts/{accountId}/transactions"),
        (ACCOUNTS, "GET", "/accounts/{accountId}/transactions-current"),
        (ACCOUNTS, "GET", "/accounts/{accountId}/overdraft-limits"),
    ]
]
ACCOUNT_ID = re.compile("^" + ACCOUNTS + r"/accounts/([^/]+)")


def operation(method: str, path: str) -> str | None:
    """The endpoint as the requirements name it (`GET /accounts/{accountId}`), or None."""
    return next((op for m, pattern, op in ROUTES if m == method and pattern.match(path)), None)


def alta_tpm(active_consents: int) -> int:
    for up_to, tpm in ALTA_BANDS:
        if active_consents <= up_to:
            return tpm
    last_up_to, last_tpm = ALTA_BANDS[-1]
    return last_tpm + 2000 * -(-(active_consents - last_up_to) // 2_000_000)


def configured(overrides: str | None) -> dict[str, tuple[int | None, int | None]]:
    """The minimums with TRANSMITTER_LIMITS applied: JSON such as
    {"GET /accounts": {"tpm": 1200, "monthly": 10}}. A value below the minimum is refused, and so is
    a limit where none applies."""
    limits = dict(MINIMUMS)
    for op, values in json.loads(overrides or "{}").items():
        if op not in limits:
            raise ValueError(f"TRANSMITTER_LIMITS: unknown endpoint {op!r}")
        tpm, monthly = limits[op]
        for name, minimum in (("tpm", tpm), ("monthly", monthly)):
            if name not in values:
                continue
            if minimum is None:
                raise ValueError(f"TRANSMITTER_LIMITS: {op} has no {name} limit (IN 615 5.1.1, 5.2)")
            if not isinstance(values[name], int) or values[name] < minimum:
                raise ValueError(f"TRANSMITTER_LIMITS: {op} {name} must be an integer of at least {minimum}")
        limits[op] = (values.get("tpm", tpm), values.get("monthly", monthly))
    return limits


def follow_up_page(query: dict[str, str]) -> bool:
    """A page after the first: the extra requests of a pagination (IN 615 5.2)."""
    page = query.get("page", "1")
    return page.isdigit() and int(page) > 1


@dataclass(frozen=True)
class MonthlyKey:
    month: str
    endpoint: str
    obj: str
    cpf: str
    receiver: str


class Limits:
    def __init__(self, limits: dict[str, tuple[int | None, int | None]]) -> None:
        self.limits = limits
        self.lock = threading.Lock()
        self.minutes: dict[tuple[str, str, str], int] = defaultdict(int)
        self.months: dict[MonthlyKey, int] = defaultdict(int)

    def admit_traffic(self, endpoint: str, origin: str, now: datetime) -> bool:
        """Counts the request in its minute, or refuses it (429) when the minute is full."""
        tpm = self.limits[endpoint][0]
        if tpm is None:
            return True
        key = (endpoint, origin, now.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M"))
        with self.lock:
            if self.minutes[key] >= tpm:
                return False
            self.minutes[key] += 1
            return True

    def monthly_key(self, endpoint: str, path: str, consent_id: str, cpf: str, receiver: str,
                    now: datetime) -> MonthlyKey | None:
        if self.limits[endpoint][1] is None:
            return None
        account = ACCOUNT_ID.match(path)
        return MonthlyKey(now.astimezone(BRASILIA).strftime("%Y-%m"), endpoint,
                          account.group(1) if account else consent_id, cpf, receiver)

    def monthly_exhausted(self, key: MonthlyKey) -> bool:
        with self.lock:
            return self.months[key] >= self.limits[key.endpoint][1]

    def count_monthly(self, key: MonthlyKey) -> None:
        with self.lock:
            self.months[key] += 1
