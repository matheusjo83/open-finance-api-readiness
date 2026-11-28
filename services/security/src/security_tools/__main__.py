"""python -m security_tools render <dir>: the PKI (made once, kept while it has what this
version needs), the realm, Keycloak's provider jar and the suite's plan configuration, for the
in-cluster names of infra/k8s/security.yaml.
python -m security_tools run <plan> <config> <export-dir>: the conformance suite's
run-test-plan.py over one plan, timed (in the image, infra/k8s/conformance-run.yaml)."""

import argparse
import json
import os
import secrets
import subprocess
import sys
import time
from pathlib import Path

from . import evidence, pki, providers, realm, results, suite

ISSUER = f"https://keycloak.{pki.NAMESPACE}.svc/realms/{realm.REALM}"
SUITE_BASE = f"https://conformance.{pki.NAMESPACE}.svc"
# The transmitter's mTLS gateway (infra/security/gateway.conf).
RESOURCE_BASE = f"https://gateway.{pki.NAMESPACE}.svc"
# A synthetic CPF with valid check digits: the test users' and the consents the suite creates.
# Not one of the transmitter's seeded customers, so the accounts list is empty (the suite only
# needs a 200).
CPF = "76109277673"


def render(out: Path) -> None:
    tls = out / "pki"
    manifest_file = tls / "manifest.json"
    manifest = json.loads(manifest_file.read_text()) if manifest_file.exists() else None
    if manifest is None or not pki.current(manifest):
        manifest = pki.build(tls)
    (out / "realm.json").write_text(json.dumps(realm.build(manifest, SUITE_BASE, CPF), indent=1))
    # Kept like the PKI: Keycloak's bootstrap admin (the walk creates its customers' users with
    # it) and the password of those users. Local secrets, never committed.
    for name in ("keycloak-admin-password", "walker-user-password"):
        if not (out / name).exists():
            (out / name).write_text(secrets.token_urlsafe(24))
    providers.build(out)
    (out / "plan.json").write_text(json.dumps(
        suite.build(manifest, tls, issuer=ISSUER, suite_base=SUITE_BASE, resource_base=RESOURCE_BASE, cpf=CPF),
        indent=1))
    (out / "plan-name.txt").write_text(suite.plan_name() + "\n")
    print(f"rendered {out}: realm {realm.REALM}, clients {', '.join(manifest['clients'])}")


def run(plan: str, config: Path, export: Path) -> int:
    """Runs the plan, then writes and prints every module's outcome. Exit 0 when the plan ran
    and no module did other than suite.KNOWN expects (the runner's own exit code is ignored:
    it fails on any failure, expected or not)."""
    export.mkdir(parents=True, exist_ok=True)
    scripts = Path(os.environ.get("OIDF_SCRIPTS", "/app/oidf"))
    start = time.monotonic()
    uploader = evidence.Uploader(os.environ["CONFORMANCE_SERVER"])
    uploader.start()
    runner = subprocess.Popen([sys.executable, str(scripts / "run-test-plan.py"), "--export-dir", str(export),
                               "--verbose", plan, str(config)], cwd=scripts, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    output = []
    for line in runner.stdout:
        print(line, end="")
        output.append(line)
    code = runner.wait()
    uploader.stop()
    seconds = round(time.monotonic() - start, 1)
    plan_id = results.plan_id("".join(output))
    records = results.collect(os.environ["CONFORMANCE_SERVER"], plan_id) if plan_id else []
    records = results.judge(records, suite.KNOWN)
    (export / "results.json").write_text(json.dumps({"modules": records, "evidence": uploader.uploads}, indent=1))
    for r in records:
        print("MODULE " + json.dumps({"module": r["module"], "outcome": r["outcome"], "status": r["status"],
                                      "result": r["result"],
                                      "failures": sorted({f["condition"] for f in r["failures"]}),
                                      "warnings": sorted({w["condition"] for w in r["warnings"]})}))
    print("CONFORMANCE " + json.dumps({"plan": plan, "plan_id": plan_id, "runner_exit": code,
                                       "seconds": seconds, **results.summary(records),
                                       "evidence_uploaded": sorted(u["module"] for u in uploader.uploads)}))
    summary = results.summary(records)
    # The baseline gate: every module ran, and none did other than KNOWN says.
    return 0 if records and not summary["unexpected"] else 1


def main() -> None:
    parser = argparse.ArgumentParser(prog="security_tools")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("render").add_argument("out", type=Path)
    run_parser = sub.add_parser("run")
    run_parser.add_argument("plan")
    run_parser.add_argument("config", type=Path)
    run_parser.add_argument("export", type=Path)
    args = parser.parse_args()
    if args.command == "render":
        render(args.out)
    elif args.command == "run":
        sys.exit(run(args.plan, args.config, args.export))


if __name__ == "__main__":
    main()
