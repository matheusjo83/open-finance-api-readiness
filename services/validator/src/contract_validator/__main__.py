"""Entry point: python -m contract_validator (settings from the environment, docs/validator.md).
Prints the summary as one JSON line; exits 0 when the run completes, whatever it found."""

import json
import os
from pathlib import Path

import httpx2

from .auth import Keycloak
from .results import S3Store
from .spec import load_specs
from .validate import run
from .walk import Walker


def main() -> None:
    env = os.environ
    specs = load_specs(Path(env.get("SPECS_DIR", "/app/specs")))
    store = S3Store(
        bucket=env.get("RESULTS_BUCKET", "lakehouse"), endpoint=env["S3_ENDPOINT"],
        access_key=env["S3_ACCESS_KEY_ID"], secret_key=env["S3_SECRET_ACCESS_KEY"],
        region=env.get("AWS_REGION", "us-east-1"),
    )
    balance_consents = int(env.get("BALANCE_CONSENTS", "2"))
    # The proxy itself: its run, the stand-in listing GET /mock/seed, the flush.
    with httpx2.Client(base_url=env.get("INTERNAL", "http://proxy.ofb.svc"), timeout=15.0) as internal:
        if env.get("AUTH") == "keycloak":
            # The real flows, through the transmitter's mTLS gateway (auth.py).
            auth = Keycloak(issuer=env["ISSUER"], client_id=env["CLIENT_ID"],
                            directory=Path(env.get("SECURITY_DIR", "/security")))
            client = httpx2.Client(base_url=env["TARGET"], verify=auth.context(), timeout=15.0)
        else:
            auth, client = None, internal
        walker = Walker(client, balance_consents=balance_consents, auth=auth, internal=internal)
        summary = run(internal, specs, store, run_id=env.get("RUN_ID") or None, walker=walker)
        if auth is not None:
            summary["logins"] = auth.logins
        # Write the proxy's buffered labels now, so they are in the store with the results.
        try:
            internal.request("POST", "/_proxy/flush", headers={})
        except httpx2.HTTPError:
            pass
    print("CONTRACT", json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
