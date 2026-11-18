"""Entry point: python -m contract_validator (settings from the environment, docs/validator.md).
Prints the summary as one JSON line; exits 0 when the run completes, whatever it found."""

import json
import os
from pathlib import Path

import httpx2

from .results import S3Store
from .spec import load_specs
from .validate import run


def main() -> None:
    env = os.environ
    specs = load_specs(Path(env.get("SPECS_DIR", "/app/specs")))
    store = S3Store(
        bucket=env.get("RESULTS_BUCKET", "lakehouse"), endpoint=env["S3_ENDPOINT"],
        access_key=env["S3_ACCESS_KEY_ID"], secret_key=env["S3_SECRET_ACCESS_KEY"],
        region=env.get("AWS_REGION", "us-east-1"),
    )
    with httpx2.Client(base_url=env.get("TARGET", "http://proxy.ofb.svc"), timeout=15.0) as client:
        summary = run(client, specs, store, run_id=env.get("RUN_ID") or None)
        # Write the proxy's buffered labels now, so they are in the store with the results.
        try:
            client.request("POST", "/_proxy/flush", headers={})
        except httpx2.HTTPError:
            pass
    print("CONTRACT", json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
