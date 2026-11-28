"""The traffic: what a receiving institution would call, over the seeded dataset (GET /mock/seed).

For every customer: its authorised consent, the accounts list (every page), and per account the
detail, balances, overdraft limits, transactions over the last 12 months (page by page, then
again in one page of 1000) and the current transactions (every page); then each of the customer's other seeded consents (expired, revoked,
without balances, awaiting) is read and used on the accounts list and every balance; then
`balance_consents` new consents are created, authorised and read, and each reads every balance
again (a request of its own, so a draw of its own); then a consent is created, authorised, read,
renewed twice (POST /extends, with the consent's authorization_code token), its renewal
history read with each client token, the consent read again, and two renewals that must be
refused (422): of the revoked seeded consent, and to an earlier date. For the first customer, an unknown account
and a consent lifecycle (create, authorise, read, use, revoke, read again). A consent is always
read before it is used, so the business rules know its state, and read twice: the second read
carries a renewed client-credentials token, so it is a request of its own (a draw of its own)
and the state is still known when one of the two fails the contract layer. Every Open Finance
call carries a fresh x-fapi-interaction-id.

The tokens come from `auth` (auth.py): the stand-ins in the tests in process, the real FAPI-BR
flows through the gateway in the cluster. There no token can be had for a consent nobody
approved, so the seeded consent still awaiting authorisation is only read.

Pages are followed through links.next. When a full page comes without one, the next page number
is requested anyway (a careful client's probe), and the listing goes on from there.
"""

from __future__ import annotations

import json
import time
import uuid
from collections.abc import Callable, Iterator
from datetime import date, datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit

from .auth import CLIENT_TOKEN, CLIENT_TOKEN_RENEWED, StandIn  # noqa: F401 (re-exported)
from .rules import Exchange

ACCOUNTS = "/open-banking/accounts/v2"
CONSENTS = "/open-banking/consents/v3"
PERMISSIONS = ["ACCOUNTS_READ", "ACCOUNTS_BALANCES_READ", "ACCOUNTS_TRANSACTIONS_READ",
               "ACCOUNTS_OVERDRAFT_LIMITS_READ", "RESOURCES_READ"]
# The renewal's required headers: the logged user's address and agent at the receiving institution.
EXTENSION_HEADERS = {"x-fapi-customer-ip-address": "203.0.113.7", "x-customer-user-agent": "contract-validator/1.0"}
# The renewals, in days from today: two later dates, then an earlier one (refused).
RENEWAL_DAYS, EARLIER_DAYS = (180, 300), 100
# The specs' default and largest page-size.
DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE = 25, 1000


class HttpClient(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str], content: bytes | None = None) -> Any: ...


class Walker:
    def __init__(self, client: HttpClient, today: Callable[[], date] = date.today, balance_consents: int = 2,
                 auth=None, internal: HttpClient | None = None) -> None:
        """`client`: the Open Finance APIs (through the gateway in the cluster); `internal`: the
        stand-in listing GET /mock/seed (the proxy directly), `client` when not given."""
        self.client, self.today, self.balance_consents = client, today, balance_consents
        self.internal = internal or client
        self.auth = auth or StandIn(self.internal)

    def call(self, method: str, url: str, token: str | None, body: dict | None = None,
             extra: dict[str, str] | None = None) -> tuple[Exchange, Any]:
        parts = urlsplit(url)
        headers = {"x-fapi-interaction-id": str(uuid.uuid4()), **self.auth.headers(), **(extra or {})}
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        content = None
        if body is not None:
            headers["content-type"] = "application/json"
            content = json.dumps(body).encode()
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        started, t0 = datetime.now(timezone.utc), time.perf_counter()
        response = self.client.request(method, target, headers=headers, content=content)  # reads the whole body
        seconds = time.perf_counter() - t0
        ex = Exchange(method=method, path=parts.path, query=parts.query, request_headers=headers,
                      request_body=content, status=response.status_code,
                      headers={k.lower(): v for k, v in response.headers.items()}, body=response.content,
                      started=started, seconds=seconds)
        try:
            document = response.json() if "json" in ex.content_type and ex.body else None
        except ValueError:
            document = None
        return ex, document

    def pages(self, url: str, token: str) -> Iterator[Exchange]:
        """Follows links.next (made relative: the links carry the host the transmitter saw). A
        full page without a next link is followed by a probe of the next page number, and so on
        while the pages come back full."""
        seen = set()
        while url and url not in seen:
            seen.add(url)
            ex, document = self.call("GET", url, token)
            yield ex
            url = None
            if ex.status != 200 or not isinstance(document, dict):
                return
            links, data = document.get("links"), document.get("data")
            nxt = links.get("next") if isinstance(links, dict) else None
            if isinstance(nxt, str):
                parts = urlsplit(nxt)
                url = parts.path + (f"?{parts.query}" if parts.query else "")
            elif isinstance(data, list) and data:
                query = dict(parse_qsl(ex.query))
                page, size = int(query.get("page", 1)), int(query.get("page-size", DEFAULT_PAGE_SIZE))
                if len(data) >= size:
                    query.update({"page": str(page + 1), "page-size": str(size)})
                    url = f"{ex.path}?{urlencode(query)}"

    def seed(self) -> dict:
        response = self.internal.request("GET", "/mock/seed", headers={})
        response.raise_for_status()
        return response.json()

    def walk(self) -> Iterator[Exchange]:
        seed = self.seed()
        self.auth.prepare(seed)
        today = self.today()
        window = urlencode({"fromBookingDate": (today - timedelta(days=365)).isoformat(), "toBookingDate": today.isoformat()})
        current = urlencode({"fromBookingDate": (today - timedelta(days=6)).isoformat(), "toBookingDate": today.isoformat()})
        for customer in seed["customers"]:
            cpf = customer["cpf"]
            authorised = next(c for c in customer["consents"] if c.endswith("-authorised"))
            yield from self.read_consent(authorised)
            token = self.auth.consent_token(authorised, cpf)
            yield from self.pages(f"{ACCOUNTS}/accounts", token)
            for account in customer["accounts"]:
                base = f"{ACCOUNTS}/accounts/{account}"
                for suffix in ("", "/balances", "/overdraft-limits"):
                    yield self.call("GET", base + suffix, token)[0]
                yield from self.pages(f"{base}/transactions?{window}", token)
                # The whole history in one page (the spec's largest page-size), for the balance
                # reconciliation: one response to break instead of one per page.
                yield from self.pages(f"{base}/transactions?{window}&page-size={MAX_PAGE_SIZE}", token)
                yield from self.pages(f"{base}/transactions-current?{current}", token)
            # The customer's other seeded consents (expired, revoked, without balances, awaiting):
            # read, then used on the accounts list and every balance.
            for consent in customer["consents"]:
                if consent == authorised:
                    continue
                yield from self.read_consent(consent)
                if consent.endswith("-awaiting") and not self.auth.tokens_for_unapproved:
                    continue
                other = self.auth.consent_token(consent, cpf)
                yield self.call("GET", f"{ACCOUNTS}/accounts", other)[0]
                for account in customer["accounts"]:
                    yield self.call("GET", f"{ACCOUNTS}/accounts/{account}/balances", other)[0]
            # More balance reads, each with a consent of its own (a request of its own).
            for _ in range(self.balance_consents):
                consent = None
                for ex, consent in self.authorised_consent(cpf):
                    yield ex
                if consent:
                    for account in customer["accounts"]:
                        yield self.call("GET", f"{ACCOUNTS}/accounts/{account}/balances",
                                        self.auth.consent_token(consent, cpf))[0]
            revoked = next((c for c in customer["consents"] if c.endswith("-revoked")), None)
            yield from self.renewals(cpf, revoked)

        first = seed["customers"][0]
        authorised = next(c for c in first["consents"] if c.endswith("-authorised"))
        yield self.call("GET", f"{ACCOUNTS}/accounts/unknown-account", self.auth.consent_token(authorised, first["cpf"]))[0]
        yield from self.lifecycle(first["cpf"])

    def read_consent(self, consent: str) -> Iterator[Exchange]:
        """Reads the consent twice, the second time with a renewed client credentials token: a
        request of its own, so a draw of its own (decision 33; the proxy counts the repeat)."""
        for renewed in (False, True):
            yield self.call("GET", f"{CONSENTS}/consents/{consent}", self.auth.client_token(renewed))[0]

    def authorised_consent(self, cpf: str) -> Iterator[tuple[Exchange, str | None]]:
        """Creates a consent for the customer, has the customer authorise it and reads it (twice).
        Yields each exchange with the consent id (None if the creation did not give one)."""
        expiration = (self.today() + timedelta(days=90)).isoformat() + "T23:59:59Z"
        body = {"data": {"loggedUser": {"document": {"identification": cpf, "rel": "CPF"}},
                         "permissions": PERMISSIONS, "expirationDateTime": expiration}}
        created, document = self.call("POST", f"{CONSENTS}/consents", self.auth.client_token(), body)
        data = (document or {}).get("data") if isinstance(document, dict) else None
        consent = data.get("consentId") if created.status == 201 and isinstance(data, dict) else None
        yield created, consent
        if consent:
            self.auth.authorise(consent, cpf)
            for ex in self.read_consent(consent):
                yield ex, consent

    def expiration(self, days: int) -> str:
        return (self.today() + timedelta(days=days)).isoformat() + "T23:59:59Z"

    def renew(self, consent: str, cpf: str, days: int) -> Exchange:
        body = {"data": {"loggedUser": {"document": {"identification": cpf, "rel": "CPF"}},
                         "expirationDateTime": self.expiration(days)}}
        return self.call("POST", f"{CONSENTS}/consents/{consent}/extends", self.auth.consent_token(consent, cpf), body,
                         EXTENSION_HEADERS)[0]

    def renewals(self, cpf: str, revoked: str | None) -> Iterator[Exchange]:
        """A consent renewed twice, its history (read with each client token, a draw each), the
        consent read again, then two renewals the transmitter must refuse."""
        consent = None
        for ex, consent in self.authorised_consent(cpf):
            yield ex
        if not consent:
            return
        for days in RENEWAL_DAYS:
            yield self.renew(consent, cpf, days)
        for renewed in (False, True):
            yield self.call("GET", f"{CONSENTS}/consents/{consent}/extensions", self.auth.client_token(renewed))[0]
        yield from self.read_consent(consent)
        if revoked:
            yield self.renew(revoked, cpf, RENEWAL_DAYS[0])
        yield self.renew(consent, cpf, EARLIER_DAYS)

    def lifecycle(self, cpf: str) -> Iterator[Exchange]:
        consent = None
        for ex, consent in self.authorised_consent(cpf):
            yield ex
        if not consent:
            return
        yield self.call("GET", f"{ACCOUNTS}/accounts", self.auth.consent_token(consent, cpf))[0]
        yield self.call("DELETE", f"{CONSENTS}/consents/{consent}", self.auth.client_token())[0]
        yield self.call("GET", f"{CONSENTS}/consents/{consent}", self.auth.client_token())[0]
