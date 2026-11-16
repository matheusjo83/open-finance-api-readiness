"""In-cluster check of the transmitter, run inside its pod (make transmitter-smoke).

Calls the Service by its cluster DNS name: the seed listing, then the accounts list and the
balances of the first seeded customer, checking statuses and the echoed interaction id.
Standard library only (the runtime image has no test dependencies).
"""

import json
import sys
import time
import urllib.error
import urllib.request
import uuid

BASE = "http://transmitter.ofb.svc"


def wait_for_service(url: str, seconds: int = 60) -> None:
    """A pod can be Ready before kube-proxy routes the Service to it (connection refused)."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5):
                return
        except (urllib.error.URLError, ConnectionError) as e:
            if time.monotonic() > deadline:
                sys.exit(f"{url} not reachable after {seconds}s: {e}")
            time.sleep(1)


def get(path: str, token: str | None = None) -> tuple[int, dict, dict]:
    interaction = str(uuid.uuid4())
    headers = {"x-fapi-interaction-id": interaction}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urllib.request.urlopen(urllib.request.Request(BASE + path, headers=headers), timeout=10) as r:
        body = json.load(r)
        if path.startswith("/open-banking") and r.headers["x-fapi-interaction-id"] != interaction:
            sys.exit(f"{path}: x-fapi-interaction-id not echoed")
        return r.status, dict(r.headers), body


wait_for_service(BASE + "/healthz")
_, _, seed = get("/mock/seed")
customer = seed["customers"][0]
token, account = customer["consents"][0], customer["accounts"][0]
status, _, accounts = get("/open-banking/accounts/v2/accounts", token)
assert status == 200 and [a["accountId"] for a in accounts["data"]] == customer["accounts"], accounts
status, _, balances = get(f"/open-banking/accounts/v2/accounts/{account}/balances", token)
assert status == 200, balances
print("TRANSMITTER", json.dumps({
    "seed": seed["seed"], "customers": len(seed["customers"]), "accounts": len(accounts["data"]),
    "available": balances["data"]["availableAmount"]["amount"],
}))
