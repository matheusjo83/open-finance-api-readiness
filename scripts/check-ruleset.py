"""Check that every ofb-* rule of the ruleset fired on the defective fixture.

Usage: python3 -I scripts/check-ruleset.py specs/ruleset.yaml <spectral-json-report>

A rule that never fires on a spec built to break it is not checking anything.
"""

import json
import re
import sys


def main(ruleset_path: str, report_path: str) -> int:
    with open(ruleset_path, encoding="utf-8") as f:
        rules = set(re.findall(r"^  (ofb-[a-z0-9-]+):", f.read(), flags=re.MULTILINE))
    with open(report_path, encoding="utf-8") as f:
        fired = {result["code"] for result in json.load(f)}

    if not rules:
        print(f"no ofb-* rules found in {ruleset_path}")
        return 1
    missing = sorted(rules - fired)
    for rule in sorted(rules):
        print(f"{'ok  ' if rule in fired else 'MISS'} {rule}")
    if missing:
        print(f"{len(missing)} rule(s) did not fire on the defective fixture: {', '.join(missing)}")
        return 1
    print(f"all {len(rules)} ofb-* rules fired on the defective fixture")
    return 0


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:]))
