"""The traffic: what a receiving institution would call, over the seeded dataset (GET /mock/seed).

For every customer: its authorised consent, the accounts list (every page), and per account the
detail, balances, overdraft limits, transactions over the last 12 months (page by page, then
again in one page of 1000) and the current transactions (every page). For the first customer, each other seeded consent is read and then
used on the accounts list; then balances with the consent that lacks the permission, an unknown
account, and a consent lifecycle (create, authorise, read, use, revoke, read again). A consent
is always read before it is used, so the business rules know its state. Every Open Finance call
carries a fresh x-fapi-interaction-id.

Pages are followed through links.next. When a full page comes without one, the next page number
is requested anyway (a careful client's probe), and the listing goes on from there.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable, Iterator
from datetime import date, timedelta
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit

from .rules import Exchange

ACCOUNTS = "/open-banking/accounts/v2"
CONSENTS = "/open-banking/consents/v3"
PERMISSIONS = ["ACCOUNTS_READ", "ACCOUNTS_BALANCES_READ", "ACCOUNTS_TRANSACTIONS_READ",
               "ACCOUNTS_OVERDRAFT_LIMITS_READ", "RESOURCES_READ"]
# The client-credentials token of the Consents API; the transmitter accepts any bearer there.
CLIENT_TOKEN = "validator-client-credentials"
# The specs' default and largest page-size.
DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE = 25, 1000


class HttpClient(Protocol):
    def request(self, method: str, url: str, *, headers: dict[str, str], content: bytes | None = None) -> Any: ...


class Walker:
    def __init__(self, client: HttpClient, today: Callable[[], date] = date.today) -> None:
        self.client, self.today = client, today

    def call(self, method: str, url: str, token: str | None, body: dict | None = None) -> tuple[Exchange, Any]:
        parts = urlsplit(url)
        headers = {"x-fapi-interaction-id": str(uuid.uuid4())}
        if token is not None:
            headers["authorization"] = f"Bearer {token}"
        content = None
        if body is not None:
            headers["content-type"] = "application/json"
            content = json.dumps(body).encode()
        target = parts.path + (f"?{parts.query}" if parts.query else "")
        response = self.client.request(method, target, headers=headers, content=content)
        ex = Exchange(method=method, path=parts.path, query=parts.query, request_headers=headers,
                      request_body=content, status=response.status_code,
                      headers={k.lower(): v for k, v in response.headers.items()}, body=response.content)
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
        response = self.client.request("GET", "/mock/seed", headers={})
        response.raise_for_status()
        return response.json()

    def walk(self) -> Iterator[Exchange]:
        seed = self.seed()
        today = self.today()
        window = urlencode({"fromBookingDate": (today - timedelta(days=365)).isoformat(), "toBookingDate": today.isoformat()})
        current = urlencode({"fromBookingDate": (today - timedelta(days=6)).isoformat(), "toBookingDate": today.isoformat()})
        for customer in seed["customers"]:
            token = next(c for c in customer["consents"] if c.endswith("-authorised"))
            yield self.call("GET", f"{CONSENTS}/consents/{token}", CLIENT_TOKEN)[0]
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

        first = seed["customers"][0]
        account = first["accounts"][0]
        for consent in first["consents"]:
            if not consent.endswith("-authorised"):
                yield self.call("GET", f"{CONSENTS}/consents/{consent}", CLIENT_TOKEN)[0]
                yield self.call("GET", f"{ACCOUNTS}/accounts", consent)[0]
        no_balances = next((c for c in first["consents"] if c.endswith("-no-balances")), None)
        if no_balances:
            yield self.call("GET", f"{ACCOUNTS}/accounts/{account}/balances", no_balances)[0]
        authorised = next(c for c in first["consents"] if c.endswith("-authorised"))
        yield self.call("GET", f"{ACCOUNTS}/accounts/unknown-account", authorised)[0]
        yield from self.lifecycle(first["cpf"])

    def lifecycle(self, cpf: str) -> Iterator[Exchange]:
        expiration = (self.today() + timedelta(days=90)).isoformat() + "T23:59:59Z"
        body = {"data": {"loggedUser": {"document": {"identification": cpf, "rel": "CPF"}},
                         "permissions": PERMISSIONS, "expirationDateTime": expiration}}
        created, document = self.call("POST", f"{CONSENTS}/consents", CLIENT_TOKEN, body)
        yield created
        consent = (document or {}).get("data", {}).get("consentId") if created.status == 201 else None
        if not consent:
            return
        self.client.request("POST", f"/mock/consents/{consent}/authorise", headers={})
        yield self.call("GET", f"{CONSENTS}/consents/{consent}", CLIENT_TOKEN)[0]
        yield self.call("GET", f"{ACCOUNTS}/accounts", consent)[0]
        yield self.call("DELETE", f"{CONSENTS}/consents/{consent}", CLIENT_TOKEN)[0]
        yield self.call("GET", f"{CONSENTS}/consents/{consent}", CLIENT_TOKEN)[0]
