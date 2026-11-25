"""In-cluster check of the telemetry, run inside the proxy's pod after the validator Job
(make telemetry-smoke).

Reads the client's and the server's PCM reports of the proxy's current run from the object store
and pairs them by fapiInteractionId, as the PCM does. Every contract result must have exactly one
client report, every client report its server report, and the two must agree on the PCM's five
pairing fields. Prints the pairing and the P95 of the client's times per endpoint.

The server also reports calls nobody reported as a client (make proxy-smoke): those are UNPAIRED.
"""

import collections
import json
import math
import os
import sys
import urllib.request

import boto3
from botocore.config import Config

PAIRING_FIELDS = ["clientOrgId", "serverOrgId", "statusCode", "httpMethod", "endpoint"]

with urllib.request.urlopen("http://localhost:8080/_proxy/run", timeout=10) as r:
    run_id = json.load(r)["run_id"]

s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], region_name=os.environ.get("AWS_REGION"),
                  aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
                  aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
                  config=Config(s3={"addressing_style": "path"}))
bucket = os.environ.get("LABELS_BUCKET", "lakehouse")


def read(prefix: str) -> list[dict]:
    records = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().decode()
            records += [json.loads(line) for line in body.splitlines()]
    return records


def by_interaction(side: str) -> dict[str, dict]:
    reports = [r["report"] for r in read(f"pcm/{side}/run={run_id}/")]
    if not reports:
        sys.exit(f"no reports under s3://{bucket}/pcm/{side}/run={run_id}/")
    out = {r.get("fapiInteractionId"): r for r in reports}
    if None in out or len(out) != len(reports):
        sys.exit(f"{side}: {len(reports)} reports, {len(out)} distinct interaction ids (None: {None in out})")
    if any(r["processTimespan"] <= 0 for r in reports):
        sys.exit(f"{side}: a report has no time")
    return out


client, server = by_interaction("client"), by_interaction("server")
# An operation outside the specification has no endpoint at the PCM, so no report.
results = {r["interaction_id"] for r in read(f"contract/run={run_id}/") if r["operation"]}
if results != set(client):
    sys.exit(f"{len(results)} contract results, {len(client)} client reports, {len(results ^ set(client))} apart")

unpaired = [i for i in client if i not in server]
inconsistent = [(i, f) for i in client if i in server for f in PAIRING_FIELDS if client[i][f] != server[i][f]]
if unpaired or inconsistent:
    sys.exit(f"{len(unpaired)} client reports unpaired, {len(inconsistent)} fields differ: {inconsistent[:5]}")

times = collections.defaultdict(list)
for r in client.values():
    times[f"{r['httpMethod']} {r['endpoint']}"].append(r["processTimespan"])
p95 = {k: sorted(v)[math.floor(0.95 * len(v) + 0.5) - 1] for k, v in sorted(times.items())}
print("TELEMETRY", json.dumps({"run_id": run_id, "paired": len(client), "server_only": len(server) - len(client),
                               "client_p95_ms": p95}))
