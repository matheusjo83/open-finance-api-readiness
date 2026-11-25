"""Check specs/operational/requirements.json against the pinned specifications and itself.

Usage: python -I scripts/check-requirements.py specs/operational/requirements.json <spec.yml>...

- every operation of the pinned specs has requirements, and no other operation does;
- each base path matches the spec's server URL;
- each endpoint agrees with its frequency class, unless it names an exception or divergence;
- only Consents go without limits;
- every block cites declared sources, every source is cited, every divergence is declared;
- the worked examples of the manual hold with the rules as written.

Then every check runs on a copy broken on purpose: a check that does not notice is not a check.
"""

from __future__ import annotations

import copy
import json
import math
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml

METHODS = ("get", "post", "put", "patch", "delete")


def half_up(value: float) -> int:
    return math.floor(round(value, 9) + 0.5)


def spec_operations(spec: dict) -> set[str]:
    return {f"{m.upper()} {path}" for path, item in spec["paths"].items() for m in METHODS if m in item}


def base_paths(spec: dict) -> set[str]:
    return {urlsplit(server["url"]).path.rstrip("/") for server in spec["servers"]}


def check_operations(req: dict, specs: dict[str, dict]) -> list[str]:
    apis, out = req["endpoints"]["apis"], []
    if set(apis) != set(specs):
        out.append(f"APIs {sorted(apis)} are not the pinned ones {sorted(specs)}")
    for name in sorted(set(apis) & set(specs)):
        ours, theirs = set(apis[name]["operations"]), spec_operations(specs[name])
        out += [f"{name}: {op} has no requirements" for op in sorted(theirs - ours)]
        out += [f"{name}: {op} is not in the specification" for op in sorted(ours - theirs)]
        if apis[name]["base_path"] not in base_paths(specs[name]):
            out.append(f"{name}: base path {apis[name]['base_path']} is not a server path of the specification")
    return out


def check_classes(req: dict, specs: dict[str, dict]) -> list[str]:
    classes, out = req["frequency_classes"]["classes"], []
    for api, entry in req["endpoints"]["apis"].items():
        for op, values in entry["operations"].items():
            cls = classes.get(values["frequency"])
            if cls is None:
                out.append(f"{api} {op}: unknown frequency {values['frequency']}")
                continue
            if values["p95_ms"] != cls["p95_ms"]:
                out.append(f"{api} {op}: p95 {values['p95_ms']} is not its class's {cls['p95_ms']}")
            for limit in ("tpm_min", "monthly_calls_min"):
                if api.startswith("consents"):
                    if values[limit] is not None:
                        out.append(f"{api} {op}: {limit} does not apply to Consents")
                elif values[limit] is None or values[limit] < cls[limit]:
                    out.append(f"{api} {op}: {limit} {values[limit]} is below its class's {cls[limit]}")
                elif values[limit] != cls[limit] and "exception" not in values:
                    out.append(f"{api} {op}: {limit} {values[limit]} differs from its class's and names no exception")
            # The manual (5) makes every Consents endpoint alta; another class must say why.
            if api.startswith("consents") and values["frequency"] != "alta" and "divergence" not in values:
                out.append(f"{api} {op}: {values['frequency']} on Consents names no divergence")
    bands = req["frequency_classes"]["alta_tpm_by_active_consents"]
    if bands != sorted(bands, key=lambda b: b["up_to"]) or bands[0]["tpm_min"] != classes["alta"]["tpm_min"]:
        out.append("the alta TPM bands are out of order or do not start at the class's value")
    return out


def check_sources(req: dict, specs: dict[str, dict]) -> list[str]:
    cited: set[str] = set()
    out = []
    blocks: dict[str, Any] = {k: v for k, v in req.items() if isinstance(v, dict) and k not in ("sources", "divergences")}
    blocks.update({f"divergences.{k}": v for k, v in req["divergences"].items()})
    for name, block in blocks.items():
        if not block.get("sources"):
            out.append(f"{name} cites no source")
        cited.update(block.get("sources", []))
    out += [f"source {s} is cited but not declared" for s in sorted(cited - set(req["sources"]))]
    out += [f"source {s} is declared but not cited" for s in sorted(set(req["sources"]) - cited)]
    for name, source in req["sources"].items():
        out += [f"source {name} has no {k}" for k in ("title", "version", "url") if not source.get(k)]
    for api in req["endpoints"]["apis"].values():
        out += [f"{op}: divergence {v['divergence']} is not declared" for op, v in api["operations"].items()
                if "divergence" in v and v["divergence"] not in req["divergences"]]
    return out


def check_examples(req: dict, specs: dict[str, dict]) -> list[str]:
    examples, rt, out = req["worked_examples"], req["response_time"], []
    for ex in examples["p95_index"]:
        if half_up(rt["percentile"] * ex["n"]) != ex["index"]:
            out.append(f"{ex['section']}: the P95 index of {ex['n']} is not {ex['index']}")
    for ex in examples["month_days"]:
        enough = ex["within_sla"] >= half_up(rt["month"]["min_share_of_days"] * ex["days"])
        compliant = enough and ex["above_sla_max_factor"] <= rt["month"]["other_days_max_factor"]
        if compliant != ex["compliant"]:
            out.append(f"{ex['section']}: the month comes out {'compliant' if compliant else 'not compliant'}")
    for key, part, rest in (("minute_availability", "success", "error"), ("daily_availability", "available", "unavailable")):
        for ex in examples[key]:
            # The manual cuts after two decimals: 255/259 is 98.4556 %, published as 98.45 %.
            if math.floor(10000 * ex[part] / (ex[part] + ex[rest])) / 100 != ex["percent"]:
                out.append(f"{key} {ex['section']}: {ex[part]}/({ex[part]}+{ex[rest]}) is not {ex['percent']} %")
    return out


CHECKS = (check_operations, check_classes, check_sources, check_examples)

ACC, CON = "accounts-2.4.2", "consents-3.3.1"


def _op(req: dict, api: str, op: str) -> dict:
    return req["endpoints"]["apis"][api]["operations"][op]


# What each check must notice: (name, check, how to break a copy).
BREAKAGES = (
    ("operation missing", check_operations, lambda r: r["endpoints"]["apis"][ACC]["operations"].pop("GET /accounts")),
    ("operation unknown", check_operations, lambda r: r["endpoints"]["apis"][ACC]["operations"].update(
        {"GET /accounts/{accountId}/reserved-balances": {}})),
    ("API missing", check_operations, lambda r: r["endpoints"]["apis"].pop(CON)),
    ("wrong base path", check_operations, lambda r: r["endpoints"]["apis"][ACC].update(base_path="/open-banking/accounts/v1")),
    ("unknown frequency", check_classes, lambda r: _op(r, ACC, "GET /accounts").update(frequency="rara")),
    ("p95 off its class", check_classes, lambda r: _op(r, ACC, "GET /accounts").update(p95_ms=1500)),
    ("limit below its class", check_classes, lambda r: _op(r, ACC, "GET /accounts").update(monthly_calls_min=4)),
    ("exception not named", check_classes, lambda r: _op(r, ACC, "GET /accounts/{accountId}/balances").pop("exception")),
    ("limit on Consents", check_classes, lambda r: _op(r, CON, "POST /consents").update(tpm_min=2500)),
    ("limit missing on Accounts", check_classes, lambda r: _op(r, ACC, "GET /accounts").update(tpm_min=None)),
    ("divergence not named", check_classes, lambda r: _op(r, CON, "POST /consents/{consentId}/extends").pop("divergence")),
    ("bands out of order", check_classes, lambda r: r["frequency_classes"]["alta_tpm_by_active_consents"].reverse()),
    ("block without source", check_sources, lambda r: r["availability"].pop("sources")),
    ("source not declared", check_sources, lambda r: r["sources"].pop("in-706")),
    ("source not cited", check_sources, lambda r: r["sources"].update(x={"title": "x", "version": "x", "url": "x"})),
    ("source without version", check_sources, lambda r: r["sources"]["in-615"].pop("version")),
    ("divergence not declared", check_sources, lambda r: r["divergences"].pop("consents-renewal-frequency")),
    ("percentile changed", check_examples, lambda r: r["response_time"].update(percentile=0.9)),
    ("share of days changed", check_examples, lambda r: r["response_time"]["month"].update(min_share_of_days=0.95)),
    ("factor changed", check_examples, lambda r: r["response_time"]["month"].update(other_days_max_factor=1.5)),
    ("minute example changed", check_examples, lambda r: r["worked_examples"]["minute_availability"][0].update(error=5)),
    ("daily example changed", check_examples, lambda r: r["worked_examples"]["daily_availability"][0].update(unavailable=80)),
)


def main(requirements_path: str, *spec_paths: str) -> int:
    req = json.loads(Path(requirements_path).read_text(encoding="utf-8"))
    # utf-8-sig: both files start with a byte order mark.
    specs = {Path(p).stem: yaml.safe_load(Path(p).read_text(encoding="utf-8-sig")) for p in spec_paths}

    problems = [p for check in CHECKS for p in check(req, specs)]
    for problem in problems:
        print(f"FAIL {problem}")
    if problems:
        return 1

    missed = []
    for name, check, breakage in BREAKAGES:
        broken = copy.deepcopy(req)
        breakage(broken)
        if not check(broken, specs):
            missed.append(name)
            print(f"MISS {name}")
    if missed:
        print(f"{len(missed)} breakage(s) went unnoticed")
        return 1

    operations = sum(len(a["operations"]) for a in req["endpoints"]["apis"].values())
    print(f"{operations} operations, {len(req['sources'])} sources, {len(req['divergences'])} divergences: ok; "
          f"all {len(BREAKAGES)} breakages noticed")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
