"""In-cluster check of the defect proxy, run inside its pod (make proxy-smoke).

Walks every seeded resource through http://proxy.ofb.svc, flushes the labels, reads them back
from the object store and checks there is one label per call, keyed by the interaction id
sent, with at least one defect. Prints the defects per class.
"""

import collections
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid

import boto3
from botocore.config import Config

BASE = "http://proxy.ofb.svc"
ACCOUNTS = "/open-banking/accounts/v2"


def wait_for_service(url: str, seconds: int = 60) -> None:
    """A pod can be Ready before kube-proxy routes the Service to it (connection refused).
    /mock/seed goes through the proxy to the transmitter, so it checks both hops."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            with urllib.request.urlopen(url, timeout=5):
                return
        except (urllib.error.URLError, ConnectionError) as e:
            if time.monotonic() > deadline:
                sys.exit(f"{url} not reachable after {seconds}s: {e}")
            time.sleep(1)


def run_id(base: str) -> str | None:
    try:
        with urllib.request.urlopen(base + "/_proxy/run", timeout=5) as r:
            return json.load(r)["run_id"]
    except (urllib.error.URLError, ConnectionError, TimeoutError):
        return None


def wait_for_this_pod(seconds: int = 90, in_a_row: int = 5) -> None:
    """Right after a rollout the Service may still route to the old pod, whose labels go to
    another run: wait until it answers with this pod's run id several times in a row."""
    mine, deadline, seen = run_id("http://localhost:8080"), time.monotonic() + seconds, 0
    while seen < in_a_row:
        seen = seen + 1 if mine and run_id(BASE) == mine else 0
        if time.monotonic() > deadline:
            sys.exit(f"{BASE} does not answer with this pod's run {mine} after {seconds}s")
        time.sleep(0.5 if seen else 1)


def call(path: str, token: str | None = None, method: str = "GET", attempts: int = 3) -> tuple[int, dict]:
    """A connection that fails or times out is tried again with the same interaction id."""
    headers = {"x-fapi-interaction-id": str(uuid.uuid4())}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(BASE + path, headers=headers, method=method)
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(request, timeout=10) as r:
                return r.status, {"interaction": headers["x-fapi-interaction-id"], "body": json.load(r)}
        except urllib.error.HTTPError as e:
            return e.code, {"interaction": headers["x-fapi-interaction-id"], "body": None}
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            if attempt == attempts - 1:
                raise
            time.sleep(2)


wait_for_service(BASE + "/mock/seed")
wait_for_this_pod()
sent = []
_, seed = call("/mock/seed")
for customer in seed["body"]["customers"]:
    token = customer["consents"][0]
    paths = [f"{ACCOUNTS}/accounts"]
    for account in customer["accounts"]:
        base = f"{ACCOUNTS}/accounts/{account}"
        paths += [base, f"{base}/balances", f"{base}/overdraft-limits", f"{base}/transactions-current"]
    for path in paths:
        sent.append(call(path, token)[1]["interaction"])
for consent in seed["body"]["customers"][0]["consents"][1:]:
    sent.append(call(f"{ACCOUNTS}/accounts", consent)[1]["interaction"])

_, flushed = call("/_proxy/flush", method="POST")
prefix = flushed["body"]["labels_prefix"]
s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], region_name=os.environ.get("AWS_REGION"),
                  aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
                  aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
                  config=Config(s3={"addressing_style": "path"}))
bucket = os.environ.get("LABELS_BUCKET", "lakehouse")
records = []
for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
    for obj in page.get("Contents", []):
        body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode()
        records += [json.loads(line) for line in body.splitlines()]

by_interaction = {r["interaction_id"]: r for r in records}
missing = [i for i in sent if i not in by_interaction]
if missing:
    sys.exit(f"{len(missing)} of {len(sent)} calls have no label under s3://{bucket}/{prefix}")
defects = collections.Counter(by_interaction[i]["defect_class"] for i in sent if by_interaction[i]["defect_class"])
if not defects:
    sys.exit("no defect injected in the walk")
print("PROXY", json.dumps({"labels": f"s3://{bucket}/{prefix}", "calls": len(sent),
                           "defects": sum(defects.values()), "by_class": dict(sorted(defects.items()))}))
