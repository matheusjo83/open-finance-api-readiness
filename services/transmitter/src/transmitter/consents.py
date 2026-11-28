"""Consent store and lifecycle (Consents API 3.3.1).

Transitions, applied lazily whenever a consent is read:
- POST creates AWAITING_AUTHORISATION;
- the customer's approval at the transmitter (mock endpoint, called by the authorisation server
  when it issues a token for the consent, or by the walk) moves it to AUTHORISED;
- AWAITING_AUTHORISATION for more than 60 minutes becomes REJECTED (ASPSP, CONSENT_EXPIRED);
- AUTHORISED past expirationDateTime becomes REJECTED (ASPSP, CONSENT_MAX_DATE_REACHED);
- DELETE moves AWAITING_AUTHORISATION or AUTHORISED to REJECTED (USER, CUSTOMER_MANUALLY_REVOKED,
  or CUSTOMER_MANUALLY_REJECTED before authorisation);
- POST /extends on an AUTHORISED consent changes its expirationDateTime (or makes it
  indeterminate) and records the extension; its history is GET /extensions, newest first.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from .dataset import Dataset

AWAITING_TIMEOUT = timedelta(minutes=60)
MAX_VALIDITY = timedelta(days=365)

# Permissions the accounts endpoints need (Accounts 2.4.2, "Permissions necessárias").
ACCOUNTS_PERMISSIONS = [
    "ACCOUNTS_READ",
    "ACCOUNTS_BALANCES_READ",
    "ACCOUNTS_TRANSACTIONS_READ",
    "ACCOUNTS_OVERDRAFT_LIMITS_READ",
    "RESOURCES_READ",
]


@dataclass
class Rejection:
    rejected_by: str
    code: str


@dataclass
class Extension:
    """One renewal: the expiration it set and the one it replaced (None: indeterminate)."""

    requested: datetime
    expiration: datetime | None
    previous: datetime | None
    cpf: str
    ip_address: str
    user_agent: str


@dataclass
class Consent:
    consent_id: str
    cpf: str
    cnpj: str | None
    permissions: list[str]
    created: datetime
    expiration: datetime | None
    status: str = "AWAITING_AUTHORISATION"
    status_updated: datetime | None = None
    rejection: Rejection | None = None
    linked: bool | None = None
    extensions: list[Extension] = field(default_factory=list)
    # The receiver's client that created it (through the gateway); None for the stand-ins.
    client_id: str | None = None

    def __post_init__(self) -> None:
        if self.status_updated is None:
            self.status_updated = self.created

    def reject(self, at: datetime, by: str, code: str) -> None:
        self.status, self.status_updated, self.rejection = "REJECTED", at, Rejection(by, code)

    def refresh(self, now: datetime) -> None:
        if self.status == "AWAITING_AUTHORISATION" and now - self.created > AWAITING_TIMEOUT:
            self.reject(self.created + AWAITING_TIMEOUT, "ASPSP", "CONSENT_EXPIRED")
        elif self.status == "AUTHORISED" and self.expiration is not None and now > self.expiration:
            self.reject(self.expiration, "ASPSP", "CONSENT_MAX_DATE_REACHED")


@dataclass
class ConsentStore:
    consents: dict[str, Consent] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def get(self, consent_id: str, now: datetime) -> Consent | None:
        with self.lock:
            consent = self.consents.get(consent_id)
            if consent is not None:
                consent.refresh(now)
            return consent

    def add(self, consent: Consent) -> Consent:
        with self.lock:
            self.consents[consent.consent_id] = consent
            return consent

    def create(self, cpf: str, cnpj: str | None, permissions: list[str], expiration: datetime | None,
               linked: bool | None, now: datetime, client_id: str | None = None) -> Consent:
        return self.add(Consent(
            consent_id=f"urn:transmitter:{uuid.uuid4()}",
            cpf=cpf, cnpj=cnpj, permissions=permissions, created=now, expiration=expiration, linked=linked,
            client_id=client_id,
        ))


def seed_consents(dataset: Dataset, now: datetime) -> list[Consent]:
    """Deterministic consents for the seeded customers (listed by GET /mock/seed).

    Every customer gets, in this order: an AUTHORISED consent with all accounts permissions,
    valid for a year, then one consent per non-happy path: expired, revoked, without balances
    permission and still awaiting authorisation.
    """
    out: list[Consent] = []
    for index, customer in enumerate(dataset.customers, start=1):
        prefix, cpf = f"urn:transmitter:seed-{index}", customer.cpf
        out.append(Consent(
            consent_id=f"{prefix}-authorised", cpf=cpf, cnpj=None,
            permissions=list(ACCOUNTS_PERMISSIONS), created=now - timedelta(days=10),
            expiration=now + MAX_VALIDITY - timedelta(days=10), status="AUTHORISED",
            status_updated=now - timedelta(days=10, minutes=-5),
        ))
        out.append(Consent(
            consent_id=f"{prefix}-expired", cpf=cpf, cnpj=None,
            permissions=list(ACCOUNTS_PERMISSIONS), created=now - timedelta(days=400),
            expiration=now - timedelta(days=35), status="AUTHORISED",
            status_updated=now - timedelta(days=400, minutes=-5),
        ))
        revoked = Consent(
            consent_id=f"{prefix}-revoked", cpf=cpf, cnpj=None,
            permissions=list(ACCOUNTS_PERMISSIONS), created=now - timedelta(days=60),
            expiration=now + timedelta(days=300), status="AUTHORISED",
        )
        revoked.reject(now - timedelta(days=20), "USER", "CUSTOMER_MANUALLY_REVOKED")
        out.append(revoked)
        out.append(Consent(
            consent_id=f"{prefix}-no-balances", cpf=cpf, cnpj=None,
            permissions=["ACCOUNTS_READ", "RESOURCES_READ"], created=now - timedelta(days=5),
            expiration=now + timedelta(days=180), status="AUTHORISED",
            status_updated=now - timedelta(days=5, minutes=-5),
        ))
        out.append(Consent(
            consent_id=f"{prefix}-awaiting", cpf=cpf, cnpj=None,
            permissions=list(ACCOUNTS_PERMISSIONS), created=now - timedelta(minutes=1),
            expiration=now + timedelta(days=90),
        ))
    return out
