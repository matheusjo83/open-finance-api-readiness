"""Collects the current run's data from the object store, run inside the proxy's pod
(make reports). Writes a gzipped tar to stdout with the run's labels, contract results,
exchanges and detection report under their object keys, and run.json (the proxy's settings and
counters)."""

import io
import json
import os
import sys
import tarfile
import urllib.request

import boto3
from botocore.config import Config

with urllib.request.urlopen("http://localhost:8080/_proxy/run", timeout=10) as r:
    run = json.load(r)
run_id = run["run_id"]

s3 = boto3.client("s3", endpoint_url=os.environ["S3_ENDPOINT"], region_name=os.environ.get("AWS_REGION"),
                  aws_access_key_id=os.environ["S3_ACCESS_KEY_ID"],
                  aws_secret_access_key=os.environ["S3_SECRET_ACCESS_KEY"],
                  config=Config(s3={"addressing_style": "path"}))
bucket = os.environ.get("LABELS_BUCKET", "lakehouse")
prefixes = [f"labels/run={run_id}/", f"contract/run={run_id}/", f"exchanges/run={run_id}/", f"reports/{run_id}/"]


def add(tar: tarfile.TarFile, name: str, body: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(body)
    tar.addfile(info, io.BytesIO(body))


with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as tar:
    add(tar, "run.json", json.dumps(run, indent=1).encode())
    for prefix in prefixes:
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
            for obj in page.get("Contents", []):
                add(tar, obj["Key"], s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read())
