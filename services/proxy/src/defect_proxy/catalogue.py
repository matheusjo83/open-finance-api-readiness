"""Defect catalogue: what the proxy can break, where, and which layer should notice.

Each class says when it applies to an exchange (route, status, body) and how it mutates the
response. Contract defects break the specification; business defects keep the response valid
against the specification and break a rule of the domain (docs/transmitter.md, Invariants), so
each layer is measured on its own. docs/defects.md describes every class.
"""

from __future__ import annotations

import copy
import random
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

ACCOUNTS = "/open-banking/accounts/v2"
CONSENTS = "/open-banking/consents/v3"

ROUTES = [
    ("GET", re.compile(rf"^{ACCOUNTS}/accounts$"), "accounts.list"),
    ("GET", re.compile(rf"^{ACCOUNTS}/accounts/[^/]+$"), "accounts.detail"),
    ("GET", re.compile(rf"^{ACCOUNTS}/accounts/[^/]+/balances$"), "accounts.balances"),
    ("GET", re.compile(rf"^{ACCOUNTS}/accounts/[^/]+/transactions$"), "accounts.transactions"),
    ("GET", re.compile(rf"^{ACCOUNTS}/accounts/[^/]+/transactions-current$"), "accounts.transactions-current"),
    ("GET", re.compile(rf"^{ACCOUNTS}/accounts/[^/]+/overdraft-limits$"), "accounts.overdraft-limits"),
    ("POST", re.compile(rf"^{CONSENTS}/consents$"), "consents.create"),
    ("POST", re.compile(rf"^{CONSENTS}/consents/[^/]+/extends$"), "consents.extend"),
    ("GET", re.compile(rf"^{CONSENTS}/consents/[^/]+/extensions$"), "consents.extensions"),
    ("GET", re.compile(rf"^{CONSENTS}/consents/[^/]+$"), "consents.read"),
    ("DELETE", re.compile(rf"^{CONSENTS}/consents/[^/]+$"), "consents.delete"),
]
TRANSACTION_ROUTES = {"accounts.transactions", "accounts.transactions-current"}
PAGINATED_ROUTES = {"accounts.list"} | TRANSACTION_ROUTES

# Required fields per route, under /data (a list item for the list routes).
REQUIRED_FIELDS = {
    "accounts.list": ["accountId", "type", "number", "compeCode", "companyCnpj"],
    "accounts.detail": ["currency", "type", "subtype", "number"],
    "accounts.balances": ["availableAmount", "blockedAmount", "updateDateTime"],
    "accounts.transactions": ["transactionId", "creditDebitType", "transactionAmount", "transactionDateTime"],
    "accounts.transactions-current": ["transactionId", "creditDebitType", "transactionAmount", "transactionDateTime"],
    "consents.create": ["consentId", "status", "creationDateTime", "permissions"],
    "consents.read": ["consentId", "status", "creationDateTime", "permissions"],
    "consents.extend": ["consentId", "status", "creationDateTime", "permissions"],
    "consents.extensions": ["loggedUser", "requestDateTime"],
}


def route_of(method: str, path: str) -> str | None:
    return next((name for m, pattern, name in ROUTES if m == method and pattern.match(path)), None)


@dataclass
class Exchange:
    """One request through the proxy and the response it is about to return."""

    method: str
    path: str
    route: str | None
    query: list[tuple[str, str]]
    request_headers: dict[str, str]
    status: int
    headers: dict[str, str]  # lower-case names
    body: Any  # parsed JSON, or None
    upstream_status: int = 0
    replaced: Callable[[str], Awaitable[tuple[int, dict[str, str], Any]]] | None = None

    @property
    def token(self) -> str | None:
        value = self.request_headers.get("authorization", "")
        return value[7:].strip() if value.lower().startswith("bearer ") else None

    @property
    def consent(self) -> str | None:
        """The consent the request is made for: of the token's `consent:<id>` (caller.py)."""
        from .caller import consent_of
        return consent_of(self.token)

    def query_value(self, name: str) -> str | None:
        return next((v for k, v in self.query if k == name), None)

    @property
    def ok_json(self) -> bool:
        return 200 <= self.status < 300 and isinstance(self.body, dict) and "data" in self.body


@dataclass
class Injection:
    variant: str
    target: str
    detail: str


@dataclass
class Context:
    """What some defects need besides the exchange: a token for the customer's authorised
    consent (the seeded consents, by customer), and a consent's renewal history at the
    transmitter."""

    substitute_token: Callable[["Exchange"], Awaitable[str | None]]
    extensions: Callable[[str], Awaitable[list[dict]]]


@dataclass
class Defect:
    id: str
    layer: str  # contract | business
    description: str
    applies: Callable[[Exchange], bool]
    inject: Callable[[Exchange, random.Random, Context], Awaitable[Injection | None]]
    default_rate: float = 0.05
    tags: list[str] = field(default_factory=list)


def _items(ex: Exchange) -> list[tuple[str, dict]]:
    data = ex.body["data"]
    if isinstance(data, list):
        return [(f"/data/{i}", item) for i, item in enumerate(data) if isinstance(item, dict)]
    return [("/data", data)] if isinstance(data, dict) and data else []


# --- contract -------------------------------------------------------------------------------

def _missing_required_applies(ex: Exchange) -> bool:
    return ex.ok_json and ex.route in REQUIRED_FIELDS and any(
        name in item for _, item in _items(ex) for name in REQUIRED_FIELDS[ex.route]
    )


async def _missing_required(ex: Exchange, rng: random.Random, ctx: Context) -> Injection:
    candidates = [(p, item, n) for p, item in _items(ex) for n in REQUIRED_FIELDS[ex.route] if n in item]
    pointer, item, name = rng.choice(candidates)
    del item[name]
    return Injection("removed", f"{pointer}/{name}", f"required field {name} removed")


def _format_targets(ex: Exchange) -> list[tuple[str, dict, str]]:
    out: list[tuple[str, dict, str]] = []

    def walk(node: Any, pointer: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{pointer}/{key}"
                if isinstance(value, str) and (key == "amount" or key.endswith("DateTime")):
                    out.append((here, node, key))
                else:
                    walk(value, here)
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, f"{pointer}/{i}")

    walk(ex.body.get("data"), "/data")
    return out


def _invalid_format_applies(ex: Exchange) -> bool:
    return ex.ok_json and bool(_format_targets(ex))


async def _invalid_format(ex: Exchange, rng: random.Random, ctx: Context) -> Injection:
    pointer, parent, key = rng.choice(_format_targets(ex))
    before = parent[key]
    if key == "amount":
        parent[key] = before.replace(".", ",") if rng.random() < 0.5 else before[:-1]
        variant = "amount"
    else:
        parent[key] = before.replace("T", " ").rstrip("Z")
        variant = "datetime"
    return Injection(variant, pointer, f"{before!r} -> {parent[key]!r}")


def _missing_interaction_id_applies(ex: Exchange) -> bool:
    return ex.route is not None and "x-fapi-interaction-id" in ex.headers


async def _missing_interaction_id(ex: Exchange, rng: random.Random, ctx: Context) -> Injection:
    del ex.headers["x-fapi-interaction-id"]
    return Injection("removed", "header:x-fapi-interaction-id", "response header removed")


def _undeclared_field_applies(ex: Exchange) -> bool:
    return ex.ok_json and bool(_items(ex))


async def _undeclared_field(ex: Exchange, rng: random.Random, ctx: Context) -> Injection:
    pointer, item = rng.choice(_items(ex))
    item["internalReference"] = f"{rng.getrandbits(48):012x}"
    return Injection("added", f"{pointer}/internalReference", "field not declared in the specification")


# --- business -------------------------------------------------------------------------------

def _inconsistent_balance_applies(ex: Exchange) -> bool:
    return ex.ok_json and ex.route == "accounts.balances" and "availableAmount" in ex.body["data"]


async def _inconsistent_balance(ex: Exchange, rng: random.Random, ctx: Context) -> Injection:
    amount = ex.body["data"]["availableAmount"]
    before = Decimal(amount["amount"])
    delta = Decimal(rng.randrange(1, 50000)) / 100 * rng.choice([1, -1])
    amount["amount"] = f"{before + delta:.2f}"
    return Injection("shifted", "/data/availableAmount/amount", f"{before} -> {amount['amount']} ({delta:+})")


def _pagination_variants(ex: Exchange) -> list[str]:
    if not (ex.ok_json and ex.route in PAGINATED_ROUTES and isinstance(ex.body["data"], list)):
        return []
    variants = []
    if len(ex.body["data"]) >= 2:
        variants.append("duplicate-item")
    if "next" in ex.body.get("links", {}):
        variants.append("missing-next")
    if ex.route == "accounts.list" and "totalRecords" in ex.body.get("meta", {}):
        variants.append("wrong-total")
    return variants


async def _broken_pagination(ex: Exchange, rng: random.Random, ctx: Context) -> Injection:
    variant = rng.choice(_pagination_variants(ex))
    if variant == "duplicate-item":
        data = ex.body["data"]
        data[-1] = copy.deepcopy(data[0])
        return Injection(variant, f"/data/{len(data) - 1}", "last item replaced by a copy of the first")
    if variant == "missing-next":
        del ex.body["links"]["next"]
        return Injection(variant, "/links/next", "next link removed although more pages exist")
    meta = ex.body["meta"]
    before = meta["totalRecords"]
    meta["totalRecords"] = before + rng.randrange(1, 6)
    return Injection(variant, "/meta/totalRecords", f"{before} -> {meta['totalRecords']}")


def _consent_not_enforced_applies(ex: Exchange) -> bool:
    return (ex.route or "").startswith("accounts.") and ex.status == 401 and ex.consent is not None and ex.replaced is not None


async def _consent_not_enforced(ex: Exchange, rng: random.Random, ctx: Context) -> Injection | None:
    substitute = await ctx.substitute_token(ex)
    if substitute is None:
        return None
    status, headers, body = await ex.replaced(substitute)
    if status != 200:
        return None
    ex.status, ex.headers, ex.body = status, headers, body
    return Injection("served", "status", f"401 for consent {ex.consent} replaced by the data it should not see")


def _window(ex: Exchange) -> tuple[date, date]:
    today = datetime.now(timezone.utc).date()
    raw_from, raw_to = ex.query_value("fromBookingDate"), ex.query_value("toBookingDate")
    return (date.fromisoformat(raw_from) if raw_from else today, date.fromisoformat(raw_to) if raw_to else today)


def _outside_window_applies(ex: Exchange) -> bool:
    if not (ex.ok_json and ex.route in TRANSACTION_ROUTES and ex.body["data"]):
        return False
    try:
        _window(ex)
    except ValueError:
        return False
    return True


async def _outside_window(ex: Exchange, rng: random.Random, ctx: Context) -> Injection:
    start, end = _window(ex)
    index = rng.randrange(len(ex.body["data"]))
    item = ex.body["data"][index]
    before = item["transactionDateTime"]
    day = start - timedelta(days=rng.randrange(1, 31)) if rng.random() < 0.5 else end + timedelta(days=rng.randrange(1, 31))
    item["transactionDateTime"] = day.isoformat() + before[10:]
    return Injection("moved", f"/data/{index}/transactionDateTime",
                     f"{before} -> {item['transactionDateTime']} (window {start}..{end})")


def _shift(value: str, rng: random.Random) -> str:
    """The same RFC 3339 date-time, 1 to 30 days earlier or later."""
    moment = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    days = rng.randrange(1, 31) * rng.choice([1, -1])
    return (moment + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _history_variants(ex: Exchange) -> list[str]:
    if not (ex.ok_json and ex.route == "consents.extensions" and isinstance(ex.body["data"], list)):
        return []
    data = [item for item in ex.body["data"] if isinstance(item, dict)]
    if len(data) < 2 or len(data) != len(ex.body["data"]):
        return []
    variants = ["out-of-order"]
    if any("previousExpirationDateTime" in item for item in data[:-1]):
        variants.append("wrong-previous")
    return variants


async def _broken_history(ex: Exchange, rng: random.Random, ctx: Context) -> Injection:
    """Newest first, each renewal replacing the expiration of the next one: the list is reversed,
    or a previousExpirationDateTime (not the oldest one's) stops matching."""
    variant = rng.choice(_history_variants(ex))
    data = ex.body["data"]
    if variant == "out-of-order":
        data.reverse()
        return Injection(variant, "/data", "renewals listed oldest first")
    index = rng.choice([i for i, item in enumerate(data[:-1]) if "previousExpirationDateTime" in item])
    before = data[index]["previousExpirationDateTime"]
    data[index]["previousExpirationDateTime"] = _shift(before, rng)
    return Injection(variant, f"/data/{index}/previousExpirationDateTime",
                     f"{before} -> {data[index]['previousExpirationDateTime']}")


def _not_reflected_applies(ex: Exchange) -> bool:
    return ex.ok_json and ex.route == "consents.read" and isinstance(ex.body["data"], dict) \
        and isinstance(ex.body["data"].get("consentId"), str)


async def _not_reflected(ex: Exchange, rng: random.Random, ctx: Context) -> Injection | None:
    """A renewed consent read with the expiration its last renewal replaced. Consents never
    renewed, or last renewed from an indeterminate term, are left alone."""
    data = ex.body["data"]
    history = await ctx.extensions(data["consentId"])
    previous = history[0].get("previousExpirationDateTime") if history else None
    if previous is None or data.get("expirationDateTime") == previous:
        return None
    before = data.get("expirationDateTime")
    data["expirationDateTime"] = previous
    return Injection("previous", "/data/expirationDateTime", f"{before} -> {previous} (before the last renewal)")


CATALOGUE: list[Defect] = [
    Defect("missing-required-field", "contract", "A required field is removed from the body.",
           _missing_required_applies, _missing_required),
    Defect("invalid-format", "contract", "An amount or date-time breaks its pattern.",
           _invalid_format_applies, _invalid_format),
    Defect("missing-interaction-id", "contract", "The x-fapi-interaction-id response header is dropped.",
           _missing_interaction_id_applies, _missing_interaction_id),
    Defect("undeclared-field", "contract",
           "A field the specification does not declare is added (no schema sets additionalProperties: false).",
           _undeclared_field_applies, _undeclared_field),
    Defect("inconsistent-balance", "business", "availableAmount no longer equals the signed sum of completed transactions.",
           _inconsistent_balance_applies, _inconsistent_balance),
    Defect("broken-pagination", "business", "A page repeats an item, loses its next link or misreports totalRecords.",
           lambda ex: bool(_pagination_variants(ex)), _broken_pagination),
    Defect("consent-not-enforced", "business", "An expired, revoked or unauthorised consent is served data instead of a 401.",
           _consent_not_enforced_applies, _consent_not_enforced, default_rate=0.5),
    Defect("transaction-outside-window", "business", "A transaction is dated outside the requested booking window.",
           _outside_window_applies, _outside_window),
    # Appended, so the classes above keep their bands of the draw (selection.py).
    Defect("broken-extension-history", "business",
           "The renewal history is out of order or a previousExpirationDateTime does not chain.",
           lambda ex: bool(_history_variants(ex)), _broken_history),
    Defect("extension-not-reflected", "business",
           "A renewed consent is read with the expiration from before its last renewal.",
           _not_reflected_applies, _not_reflected),
]
BY_ID = {d.id: d for d in CATALOGUE}
