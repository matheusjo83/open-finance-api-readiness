"""In-cluster check of a limits run, run inside the limits proxy's pod (make limits-smoke).

Reads the client's log (s3://lakehouse/limits/client/run=<run>/) and the proxy's labels of the
same run, and checks that every request has its label with the status the client got, that each
traffic burst stayed in one calendar minute, and that the clean cases met the transmitter's
limits exactly where the minimums put them: the request after the TPM got 429 and none before;
the success after the monthly minimum got 423 and none before; Consents never. Prints the cases
and the defects per class and variant.
"""

import collections
import json
import os
import sys
import urllib.request

import boto3
from botocore.config import Config

TPM = {"accounts.list": 1000, "accounts.detail": 1000, "accounts.balances": 2500, "accounts.transactions": 1000,
       "accounts.transactions-current": 2500, "accounts.overdraft-limits": 2500}
MONTHLY = {"accounts.list": 8, "accounts.detail": 8, "accounts.balances": 420, "accounts.transactions": 8,
           "accounts.transactions-current": 240, "accounts.overdraft-limits": 420}

with urllib.request.urlopen(urllib.request.Request("http://localhost:8080/_proxy/flush", method="POST")) as r:
    run_id = json.load(r)["run_id"]
s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], region_name=os.environ.get("AWS_REGION"),
                  aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
                  aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
                  config=Config(s3={"addressing_style": "path"}))
bucket = os.environ.get("LABELS_BUCKET", "lakehouse")


def read(prefix: str) -> list[dict]:
    out = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            out += [json.loads(line) for line in s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read().splitlines()]
    return out


client = read(f"limits/client/run={run_id}/")
labels = {r["interaction_id"]: r for r in read(f"labels/run={run_id}/")}
if not client:
    sys.exit(f"no client log under limits/client/run={run_id}/")
errors = []
missing = [r for r in client if r["interaction"] not in labels]
if missing:
    errors.append(f"{len(missing)} of {len(client)} requests have no label")
wrong = [r for r in client if r["interaction"] in labels and labels[r["interaction"]]["status"] != r["status"]]
if wrong:
    errors.append(f"{len(wrong)} requests got another status than their label's")

cases = collections.defaultdict(list)
for r in sorted(client, key=lambda r: r["start"]):
    cases[r["case"]].append(r)
defects = collections.Counter()
clean = collections.Counter()
for case, rows in sorted(cases.items()):
    drawn = {(labels[r["interaction"]]["defect_class"], labels[r["interaction"]]["defect_variant"])
             for r in rows if r["interaction"] in labels and labels[r["interaction"]]["defect_class"]}
    defects.update(f"{c}/{v}" for c, v in drawn)
    kind, route = rows[0]["kind"], rows[0]["route"]
    statuses = [r["status"] for r in rows]
    if kind == "traffic":
        minutes = {r["start"] // 60000 for r in rows}
        if len(minutes) != 1:
            errors.append(f"{case}: the burst spans {len(minutes)} minutes "
                          f"({(rows[-1]['start'] - rows[0]['start']) / 1000:.1f} s)")
        elif not drawn and (429 in statuses[:TPM[route]] or statuses[TPM[route]] != 429):
            errors.append(f"{case}: clean, but the first 429 is request {statuses.index(429) + 1 if 429 in statuses else None}")
    elif kind == "operational" and not drawn:
        firsts = [r["status"] for r in rows if r["page"] == 1 and r["path"] == rows[0]["path"]]
        ok = sum(1 for s in firsts if 200 <= s < 300)
        if ok != MONTHLY[route] or firsts[ok] != 423 or 423 in firsts[:ok]:
            errors.append(f"{case}: clean, but {ok} successes before the first 423")
    elif kind == "consents" and not drawn and {429, 423} & set(statuses):
        errors.append(f"{case}: clean, but refused on Consents")
    if not drawn:
        clean[kind] += 1
if not defects:
    errors.append("no limit defect in the run")
summary = {"run_id": run_id, "requests": len(client), "cases": len(cases), "clean": dict(sorted(clean.items())),
           "defects": dict(sorted(defects.items())),
           "traffic_burst_seconds": {c: round((rows[-1]["start"] - rows[0]["start"]) / 1000, 1)
                                     for c, rows in sorted(cases.items()) if rows[0]["kind"] == "traffic"}}
print("LIMITS-SMOKE", json.dumps(summary))
if errors:
    sys.exit("\n".join(errors))
