"""In-cluster check of the contract validator, run inside the proxy's pod after the validator Job
(make validator-smoke).

Reads the proxy's run id, then the validator's results and the proxy's labels for that run from
the object store, and joins them by interaction id. Every result must have its label, every
contract defect must be flagged and nothing else may be: the validator over the proxy's
catalogue is exact by construction (services/validator/tests). Prints the outcome per class.
"""

import collections
import json
import os
import sys
import urllib.request

import boto3
from botocore.config import Config

with urllib.request.urlopen("http://proxy.ofb.svc/_proxy/run", timeout=10) as r:
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


results = read(f"contract/run={run_id}/")
exchanges = read(f"exchanges/run={run_id}/")
labels = {r["interaction_id"]: r for r in read(f"labels/run={run_id}/")}
if not results:
    sys.exit(f"no results under s3://{bucket}/contract/run={run_id}/")
if len(exchanges) != len(results):
    sys.exit(f"{len(results)} results but {len(exchanges)} exchanges")
unlabelled = [r["interaction_id"] for r in results if r["interaction_id"] not in labels]
if unlabelled:
    sys.exit(f"{len(unlabelled)} of {len(results)} results have no label")

outcome = collections.Counter()
wrong = []
for r in results:
    label = labels[r["interaction_id"]]
    flagged, contract = bool(r["findings"]), label["defect_layer"] == "contract"
    outcome[(label["defect_class"] or "clean", "flagged" if flagged else "passed")] += 1
    if flagged != contract:
        wrong.append({"class": label["defect_class"], "path": r["path"], "rules": r["rules"]})
by_class = collections.defaultdict(dict)
for (cls, verdict), n in sorted(outcome.items()):
    by_class[cls][verdict] = n
print("VALIDATOR", json.dumps({"run_id": run_id, "results": len(results), "by_class": by_class}))
if wrong:
    sys.exit(f"{len(wrong)} results disagree with the labels: {json.dumps(wrong[:10])}")
if not any(label["defect_layer"] == "contract" for label in (labels[r["interaction_id"]] for r in results)):
    sys.exit("no contract defect in the walk")
