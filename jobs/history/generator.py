"""A seeded history of PCM reports, with operational defects built in (docs/history.md).

The unit is an endpoint on a day (Brasília time). Each unit is generated on its own from the
seed, the endpoint and the day, so the units can be spread over Spark's executors and a day
comes out the same whatever else is generated. A unit gets at most one episode, from the widest
scope that has one: the institution's day (overload), the API's day (reporting), then the
endpoint's day (slow day, outage, edges). Chronic outages are chosen per endpoint, on days
without an episode.

Every unit also returns its ledger: the counts the reports were built from (requests per minute
and outcome, the time at the P95 rank, 529s, reports missing or late). The episodes are built to
cross the thresholds of the requirements, and the ledger checks they did: a unit that misses its
target raises. The labels are the ledgers and the episodes, never a reading of the reports.
"""

from __future__ import annotations

import hashlib
import json
import math
import random
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterator

BRT = timezone(timedelta(hours=-3))  # Brasília: no daylight saving time since 2019
CLIENT_ORG = str(uuid.uuid5(uuid.NAMESPACE_DNS, "receiver.ofb.svc"))
SERVER_ORG = str(uuid.uuid5(uuid.NAMESPACE_DNS, "transmitter.ofb.svc"))
CLIENT_SS = str(uuid.uuid5(uuid.NAMESPACE_DNS, "software-statement.receiver.ofb.svc"))
URI_PREFIX = "https://proxy.ofb.svc"

# Per scope, the classes in draw order with their default rates: (class, variants).
INSTITUTION_CLASSES = [("overload", ["over-0.5", "over-5"])]
API_CLASSES = [("unpaired", ["late-client", "late-server", "missing-some-client", "missing-some-server",
                             "missing-many-client", "missing-many-server"]),
               ("inconsistent-pair", ["status", "endpoint"])]
ENDPOINT_CLASSES = [("slow-day", ["mild", "severe"]), ("outage", ["daily"])]
CLASSES = [c for c, _ in INSTITUTION_CLASSES + API_CLASSES + ENDPOINT_CLASSES] + ["outage-chronic"]
# Edges: days at a threshold, with the verdict the requirements give there.
INSTITUTION_EDGES = ["overload-at-0.5"]
ENDPOINT_EDGES = ["p95-at-sla", "minute-at-95", "day-at-95"]
DEFAULT_RATES = {"overload": 0.04, "unpaired": 0.06, "inconsistent-pair": 0.03, "slow-day": 0.05, "outage": 0.03,
                 "edge": 0.03, "chronic-endpoints": 0.25}
CHRONIC_DAYS = 15


@dataclass(frozen=True)
class Endpoint:
    api: str  # e.g. accounts
    method: str
    endpoint: str  # the PCM's template, with the base path
    p95_ms: int

    @property
    def key(self) -> str:
        return f"{self.method} {self.endpoint}"

    @property
    def success(self) -> int:
        if self.method == "DELETE":
            return 204
        return 201 if self.method == "POST" else 200


def endpoints(requirements: dict) -> list[Endpoint]:
    out = []
    for name, api in requirements["endpoints"]["apis"].items():
        for op, values in api["operations"].items():
            method, path = op.split(" ", 1)
            out.append(Endpoint(name.split("-")[0], method, api["base_path"] + path, values["p95_ms"]))
    return out


@dataclass(frozen=True)
class Settings:
    seed: int = 1
    end: date = date(2026, 11, 15)
    days: int = 90
    minutes: int = 30  # minutes with traffic per endpoint and day
    per_minute: int = 3  # requests in each of those minutes
    rates: dict = field(default_factory=lambda: dict(DEFAULT_RATES))

    @property
    def run_id(self) -> str:
        return f"history-s{self.seed}-{self.days}d-{self.minutes}x{self.per_minute}"

    def day_list(self) -> list[date]:
        return [self.end - timedelta(days=self.days - 1 - i) for i in range(self.days)]


def _draw(*parts: Any) -> random.Random:
    digest = hashlib.sha256("|".join(map(str, parts)).encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))


def _pick(rng: random.Random, classes: list[tuple[str, list[str]]], rates: dict, edges: list[str]) -> tuple:
    """(class, variant) or ("edge", name) or (None, None): one uniform draw over the rates."""
    u, acc = rng.random(), 0.0
    for cls, variants in classes:
        acc += rates.get(cls, 0.0)
        if u < acc:
            return cls, rng.choice(variants)
    acc += rates.get("edge", 0.0) if edges else 0.0
    if edges and u < acc:
        return "edge", rng.choice(edges)
    return None, None


def institution_episode(s: Settings, day: date) -> tuple:
    return _pick(_draw(s.seed, "institution", day), INSTITUTION_CLASSES, s.rates, INSTITUTION_EDGES)


def api_episode(s: Settings, api: str, day: date) -> tuple:
    return _pick(_draw(s.seed, "api", api, day), API_CLASSES, s.rates, [])


def own_episode(s: Settings, ep: Endpoint, day: date) -> tuple:
    return _pick(_draw(s.seed, "endpoint", ep.key, day), ENDPOINT_CLASSES, s.rates, ENDPOINT_EDGES)


def episode(s: Settings, ep: Endpoint, day: date) -> tuple[str | None, str | None, str | None]:
    """(scope, class, variant) of the unit: the widest scope with an episode wins."""
    for scope, (cls, variant) in (("institution", institution_episode(s, day)), ("api", api_episode(s, ep.api, day)),
                                  ("endpoint", own_episode(s, ep, day))):
        if cls is not None:
            return scope, cls, variant
    return None, None, None


def chronic_days(s: Settings, ep: Endpoint) -> set[date]:
    """The days of a chronic outage, if the endpoint has one: days without an episode."""
    if _draw(s.seed, "chronic", ep.key).random() >= s.rates.get("chronic-endpoints", 0.0):
        return set()
    free = [d for d in s.day_list() if episode(s, ep, d)[1] is None]
    return set(sorted(free, key=lambda d: _draw(s.seed, "chronic-day", ep.key, d).random())[:CHRONIC_DAYS])


def chronic_minutes(s: Settings) -> int:
    """Unavailable minutes per chronic day: the day stays at 95 % or more, the 90-day mean falls
    below 99.5 % with CHRONIC_DAYS such days."""
    k = math.floor(0.005 * s.days * s.minutes / CHRONIC_DAYS) + 1
    if k > math.floor(0.05 * s.minutes):
        raise ValueError(f"{s.minutes} minutes a day are too few for a chronic outage")
    return k


def half_up(value: float) -> int:
    return math.floor(round(value, 9) + 0.5)


@dataclass
class Call:
    minute: int  # minute of the day, Brasília
    status: int  # what the client got
    client: bool = True  # the client reports it (eventually)
    server: bool = True
    late: str | None = None  # "client" or "server": that side reports after 08:00 of the next day
    inconsistent: str | None = None  # "status" or "endpoint": the server reports another value
    zero_time: bool = False  # a report with processTimespan 0 (left out of the response time)
    ms: int = 0  # the counted time

    @property
    def counted(self) -> bool:
        """In the institution's metrics: not PAIRED_INCONSISTENT, reported by someone."""
        return self.inconsistent is None and (self.client or self.server)


def _valid(status: int) -> str | None:
    if 200 <= status < 300 or status == 422:
        return "success"
    if 500 <= status < 600 or status == 408:
        return "error"
    return None


ERRORS = [500, 502, 503, 504, 408]


def build_calls(s: Settings, ep: Endpoint, day: date, scope, cls, variant, chronic: bool,
                rng: random.Random) -> list[Call]:
    edge = variant if cls == "edge" else None
    minutes = s.minutes
    if edge == "day-at-95":
        minutes = max(20, 20 * math.ceil(s.minutes / 20))  # 95 % of it is a whole number
    chosen = sorted(rng.sample(range(1440), min(minutes, 1440)))
    calls = []
    for m in chosen:
        for i in range(s.per_minute):
            call = Call(m, ep.success)
            # Noise: invalid 404s, 422s and times of 0. Never on edges (they stay exact), nor on a
            # minute's first call, so no minute loses all its valid requests.
            if edge is None and i > 0:
                r = rng.random()
                call.status = 404 if r < 0.02 else 422 if r < 0.04 else ep.success
                call.zero_time = rng.random() < 0.01
            calls.append(call)
    by_minute: dict[int, list[Call]] = {}
    for call in calls:
        by_minute.setdefault(call.minute, []).append(call)

    def fail_minutes(count: int) -> None:
        for m in rng.sample(chosen, count):
            for call in by_minute[m]:
                call.status = rng.choice(ERRORS)

    if cls == "outage":
        fail_minutes(math.floor(0.05 * minutes) + 1 + rng.randrange(3))
    elif chronic:
        fail_minutes(chronic_minutes(s))
    elif edge == "day-at-95":
        fail_minutes(minutes // 20)
    elif edge == "minute-at-95":
        m = rng.choice(chosen)
        extra = [Call(m, ep.success) for _ in range(20 - len(by_minute[m]))]
        calls += extra
        by_minute[m] += extra
        by_minute[m][0].status = rng.choice(ERRORS)  # 19 of 20: exactly 95 %

    if cls == "overload" or edge == "overload-at-0.5":
        valid = sum(1 for c in calls if _valid(c.status))
        if edge:  # 529s / valid exactly 0.5 %: valid = 200 x 529s, the rest filled with successes
            n529 = max(1, math.ceil(valid / 199))
            fill = 199 * n529 - valid
        else:
            # n / (valid + n) above 0.5 % and at most 5 %, or above 5 % (up to 12 %).
            low, high = (0.005, 0.05) if variant == "over-0.5" else (0.05, 0.12)
            least = math.floor(low * valid / (1 - low)) + 1
            n529, fill = rng.randint(least, max(least, math.floor(high * valid / (1 - high)))), 0
        for _ in range(fill):
            calls.append(Call(rng.choice(chosen), ep.success))
        for _ in range(n529):
            calls.append(Call(rng.choice(chosen), 529))

    if cls == "unpaired":
        kind, side = variant.rsplit("-", 1)
        n = len(calls)
        if kind == "missing-some":  # above 1 %, at most 5 %: fails at 7 days only
            low, high = math.floor(0.01 * n) + 1, math.floor(0.05 * n)
        else:  # above 5 %
            low, high = math.floor(0.05 * n) + 1, math.floor(0.15 * n)
        if high < low:
            raise ValueError(f"{n} calls are too few for {variant}")
        for call in rng.sample(calls, rng.randint(low, high)):
            if kind == "late":
                call.late = side
            else:
                setattr(call, side, False)
    elif cls == "inconsistent-pair":
        n = len(calls)
        low = math.floor(0.01 * n) + 1
        for call in rng.sample(calls, rng.randint(low, max(low, math.floor(0.05 * n)))):
            call.inconsistent = variant
    return calls


def p95_target(ep: Endpoint, cls, variant, rng: random.Random) -> int:
    sla = ep.p95_ms
    if cls == "slow-day":
        return rng.randint(sla + 1, math.floor(1.2 * sla)) if variant == "mild" else rng.randint(math.floor(1.2 * sla) + 1, 2 * sla)
    if cls == "edge" and variant == "p95-at-sla":
        return sla
    return rng.randint(math.ceil(0.3 * sla), math.floor(0.8 * sla))


def assign_times(calls: list[Call], target: int, rng: random.Random) -> int:
    """Counted times with exactly `target` at the P95 rank. Calls out of the response time
    (529, PAIRED_INCONSISTENT, time 0) get 14 seconds, so counting them would show."""
    counted = [c for c in calls if c.counted and c.status not in (423, 429, 529) and not c.zero_time]
    for c in calls:
        c.ms = 0 if c.zero_time else 14000
    if not counted:
        return 0
    rng.shuffle(counted)
    rank = max(1, half_up(0.95 * len(counted)))
    for i, c in enumerate(counted, start=1):
        if i < rank:
            c.ms = rng.randint(max(1, target // 5), target)
        elif i == rank:
            c.ms = target
        else:
            c.ms = rng.randint(target, min(14999, max(target, math.floor(2.5 * target))))
    return len(counted)


def _ms(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def unit(s: Settings, ep: Endpoint, day: date, chronic: bool, others: list[Endpoint]) -> tuple[list[dict], list[dict], dict]:
    """(client lines, server lines, ledger) of one endpoint on one day."""
    scope, cls, variant = episode(s, ep, day)
    rng = _draw(s.seed, "unit", ep.key, day)
    calls = build_calls(s, ep, day, scope, cls, variant, chronic, rng)
    target = p95_target(ep, cls, variant, rng)
    n_rt = assign_times(calls, target, rng)

    midnight = datetime.combine(day, time(), tzinfo=BRT)
    next_8h = midnight + timedelta(days=1, hours=8)
    client_lines, server_lines = [], []
    minutes: dict[int, list[int]] = {}
    ledger = {"calls": len(calls), "unpaired_8h": 0, "unpaired_7d": 0, "inconsistent": 0, "valid": 0, "n529": 0}
    same_api = [o for o in others if o.api == ep.api and o.key != ep.key]
    for call in calls:
        # Up to 59.99 s into the minute: the server's report, 2 ms later, stays in the same minute.
        started = midnight + timedelta(minutes=call.minute, milliseconds=rng.randrange(59990))
        interaction = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        delta = 0 if call.zero_time else min(call.ms - 1, rng.randint(0, 5))
        # Most reports go within half an hour; 5 % in the last hour before the cut of 08:00 of the
        # next day, still in time.
        if rng.random() < 0.05:
            sent = next_8h - timedelta(seconds=rng.randint(1, 3600))
        else:
            sent = started + timedelta(seconds=rng.randint(5, 1800))

        def late(side: str) -> datetime:
            # From a minute after the cut to five days later (within the 7 days).
            return next_8h + timedelta(minutes=1 + rng.random() * 60 * 24 * 5) if call.late == side else sent

        common = {"httpMethod": ep.method, "statusCode": call.status, "clientOrgId": CLIENT_ORG, "serverOrgId": SERVER_ORG,
                  "fapiInteractionId": interaction}
        if call.client:
            ms = call.ms
            report = {"role": "CLIENT", "endpoint": ep.endpoint, **common, "timestamp": _ms(started),
                      "processTimespan": ms, "clientSSId": CLIENT_SS, "endpointUriPrefix": URI_PREFIX,
                      "additionalInfo": {"consentId": f"urn:history:{interaction[:8]}"}}
            client_lines.append({"run_id": s.run_id, "walk_id": "history", "sent_at": _ms(late("client")), "report": report})
        if call.server:
            server = {"role": "SERVER", "endpoint": ep.endpoint, **common,
                      "timestamp": _ms(started + timedelta(milliseconds=2)),
                      "processTimespan": call.ms if not call.client else max(0, call.ms - delta),
                      "additionalInfo": {}}
            if call.inconsistent == "status":
                server["statusCode"] = 500 if call.status < 500 else 200
            elif call.inconsistent == "endpoint":
                server["endpoint"] = (same_api[0] if same_api else ep).endpoint
            server_lines.append({"run_id": s.run_id, "sent_at": _ms(late("server")), "report": server})

        one_side = call.client != call.server
        ledger["unpaired_7d"] += one_side
        ledger["unpaired_8h"] += one_side or call.late is not None
        ledger["inconsistent"] += call.inconsistent is not None
        if call.counted:
            outcome = _valid(call.status)
            if outcome:
                ledger["valid"] += 1
                ledger["n529"] += call.status == 529
                minutes.setdefault(call.minute, [0, 0])[outcome == "error"] += 1

    available = sum(1 for ok, err in minutes.values() if ok / (ok + err) >= 0.95)
    ledger.update({
        "run_id": s.run_id, "day": day.isoformat(), "api": ep.api, "method": ep.method, "endpoint": ep.endpoint,
        "scope": scope, "class": cls if cls != "edge" else None, "variant": variant if cls != "edge" else None,
        "edge": variant if cls == "edge" else None, "chronic": chronic,
        "sla_ms": ep.p95_ms, "n_rt": n_rt, "p95_ms": target if n_rt else None,
        "minutes_available": available, "minutes_unavailable": len(minutes) - available,
    })
    check(ledger)
    return client_lines, server_lines, ledger


def daily_availability(ledger: dict) -> float | None:
    defined = ledger["minutes_available"] + ledger["minutes_unavailable"]
    return ledger["minutes_available"] / defined if defined else None


def check(ledger: dict) -> None:
    """The episode crossed the threshold it was built for, and nothing else did."""
    cls, variant, edge = ledger["class"], ledger["variant"], ledger["edge"]
    sla, p95, day = ledger["sla_ms"], ledger["p95_ms"], daily_availability(ledger)
    share529 = ledger["n529"] / ledger["valid"] if ledger["valid"] else 0.0
    unpaired_8h, unpaired_7d = (ledger[k] / ledger["calls"] for k in ("unpaired_8h", "unpaired_7d"))
    inconsistent = ledger["inconsistent"] / ledger["calls"]
    want = {
        "p95": (sla < p95 <= 1.2 * sla if variant == "mild" else p95 > 1.2 * sla) if cls == "slow-day"
        else p95 == sla if edge == "p95-at-sla" else p95 is None or p95 <= 0.8 * sla,
        "day": day < 0.95 if cls == "outage" else day == 0.95 if edge == "day-at-95"
        else (0.95 <= day < 1) if ledger["chronic"] else day is None or day >= 0.95,
        "529": share529 == 0.005 if edge == "overload-at-0.5"
        else (0.005 < share529 <= 0.05 if variant == "over-0.5" else share529 > 0.05) if cls == "overload"
        else share529 == 0,
        "8h": unpaired_8h > 0.05 if cls == "unpaired" and not variant.startswith("missing-some")
        else unpaired_8h <= 0.05,
        "7d": unpaired_7d > 0.01 if cls == "unpaired" and not variant.startswith("late") else unpaired_7d == 0,
        "inconsistent": inconsistent > 0.01 if cls == "inconsistent-pair" else inconsistent == 0,
    }
    if edge == "minute-at-95":
        want["day"] = day == 1.0  # the minute at exactly 95 % is available
    if cls == "overload" or edge == "overload-at-0.5":
        # A 529 is a 5XX, so an error in availability too (overload-in-availability): the day's
        # availability is whatever the 529s leave.
        want["day"] = True
    missed = [k for k, ok in want.items() if not ok]
    if missed:
        raise AssertionError(f"{ledger['endpoint']} {ledger['method']} {ledger['day']} {cls}/{variant or edge}: "
                             f"{missed} ({json.dumps({k: ledger[k] for k in ('p95_ms', 'minutes_available', 'minutes_unavailable', 'n529', 'valid', 'unpaired_8h', 'unpaired_7d', 'inconsistent', 'calls')})})")


def generate(s: Settings, eps: list[Endpoint], ep: Endpoint, day: date, chronic: set[date]) -> Iterator[tuple[str, str]]:
    """("client" | "server" | "ledger", JSON line) for one unit."""
    client, server, ledger = unit(s, ep, day, day in chronic, eps)
    for line in client:
        yield "client", json.dumps(line, separators=(",", ":"))
    for line in server:
        yield "server", json.dumps(line, separators=(",", ":"))
    yield "ledger", json.dumps(ledger, separators=(",", ":"))
