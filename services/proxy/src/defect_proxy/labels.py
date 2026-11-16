"""Ground-truth labels: one JSON line per proxied Open Finance exchange, clean or not.

Lines are buffered and written in parts to s3://<bucket>/labels/run=<run_id>/part-NNNNN-<host>.jsonl
(every few seconds, when the buffer fills, on POST /_proxy/flush and at shutdown). The join key
with the validators' results is the x-fapi-interaction-id the client sent.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
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

    def records(self) -> list[dict]:
        return [json.loads(line) for key in sorted(self.objects) for line in self.objects[key].splitlines()]


class LabelSink:
    def __init__(self, store: Store, run_id: str, max_buffer: int = 500) -> None:
        self.store, self.run_id, self.max_buffer = store, run_id, max_buffer
        self.buffer: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.parts = 0
        self.written = 0
        self.host = socket.gethostname()

    @property
    def prefix(self) -> str:
        return f"labels/run={self.run_id}/"

    def add(self, record: dict[str, Any]) -> bool:
        """Buffers a record; True when the buffer is full and should be flushed."""
        with self.lock:
            self.buffer.append(record)
            return len(self.buffer) >= self.max_buffer

    def flush(self) -> int:
        with self.lock:
            batch, self.buffer = self.buffer, []
            if not batch:
                return 0
            self.parts += 1
            key = f"{self.prefix}part-{self.parts:05d}-{self.host}.jsonl"
        body = "".join(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n" for r in batch).encode()
        try:
            self.store.put(key, body)
        except Exception:
            with self.lock:  # keep the records for the next attempt
                self.buffer = batch + self.buffer
            raise
        with self.lock:
            self.written += len(batch)
        return len(batch)

    async def aflush(self) -> int:
        return await asyncio.to_thread(self.flush)
