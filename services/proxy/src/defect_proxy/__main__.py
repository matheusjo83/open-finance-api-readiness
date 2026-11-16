"""Entry point: python -m defect_proxy (settings from the environment, docs/defects.md)."""

import os

import uvicorn

from .app import create_app, new_run_id
from .labels import LabelSink, S3Store
from .selection import parse_rates


def main() -> None:
    env = os.environ
    store = S3Store(
        bucket=env.get("LABELS_BUCKET", "lakehouse"), endpoint=env["S3_ENDPOINT"],
        access_key=env["S3_ACCESS_KEY_ID"], secret_key=env["S3_SECRET_ACCESS_KEY"],
        region=env.get("AWS_REGION", "us-east-1"),
    )
    sink = LabelSink(store, run_id=env.get("RUN_ID") or new_run_id())
    app = create_app(
        upstream=env.get("UPSTREAM", "http://transmitter.ofb.svc"),
        seed=int(env.get("DEFECT_SEED", "42")),
        rates=parse_rates(env.get("DEFECT_RATES")),
        sink=sink,
        flush_seconds=float(env.get("LABELS_FLUSH_SECONDS", "5")),
    )
    uvicorn.run(app, host="0.0.0.0", port=int(env.get("PORT", "8080")), log_level="info")


if __name__ == "__main__":
    main()
