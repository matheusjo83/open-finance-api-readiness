"""Where the validator writes: JSON lines in the object store, next to the proxy's labels.

- s3://<bucket>/contract/run=<run_id>/part-NNNNN-<walk_id>.jsonl: one result per exchange.
- s3://<bucket>/exchanges/run=<run_id>/part-NNNNN-<walk_id>.jsonl: the exchange itself
  (request and response), the input of the business-rules job (item 4).

The walk id keeps the parts of two walks over the same proxy run apart.
"""

from __future__ import annotations

import json
from typing import Any, Protocol


class Store(Protocol):
    def put(self, key: str, body: bytes) -> None: ...


class S3Store:
    def __init__(self, bucket: str, endpoint: str, access_key: str, secret_key: str, region: str = "us-east-1") -> None:
        import boto3  # imported here: the tests use the in-memory store
        from botocore.config import Config

        self.bucket = bucket
        self.client = boto3.client(
            "s3", endpoint_url=endpoint, aws_access_key_id=access_key, aws_secret_access_key=secret_key,
            region_name=region, config=Config(s3={"addressing_style": "path"}, retries={"max_attempts": 5}),
        )

    def put(self, key: str, body: bytes) -> None:
        self.client.put_object(Bucket=self.bucket, Key=key, Body=body, ContentType="application/x-ndjson")


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    def put(self, key: str, body: bytes) -> None:
        self.objects[key] = body

    def records(self, prefix: str = "") -> list[dict]:
        return [json.loads(line) for key in sorted(self.objects) if key.startswith(prefix)
                for line in self.objects[key].splitlines()]


class PartWriter:
    """Buffers records and writes them as numbered JSONL parts under a prefix."""

    def __init__(self, store: Store, prefix: str, walk_id: str, part_size: int = 1000) -> None:
        self.store, self.prefix, self.walk_id, self.part_size = store, prefix, walk_id, part_size
        self.buffer: list[dict[str, Any]] = []
        self.parts = 0
        self.written = 0

    def add(self, record: dict[str, Any]) -> None:
        self.buffer.append(record)
        if len(self.buffer) >= self.part_size:
            self.flush()

    def flush(self) -> None:
        if not self.buffer:
            return
        self.parts += 1
        key = f"{self.prefix}part-{self.parts:05d}-{self.walk_id}.jsonl"
        body = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in self.buffer)
        self.store.put(key, body.encode())
        self.written += len(self.buffer)
        self.buffer = []
