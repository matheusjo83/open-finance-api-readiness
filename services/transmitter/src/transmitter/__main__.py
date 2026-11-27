"""Entry point: python -m transmitter (settings from the environment)."""

import os

import uvicorn

from .app import create_app


def main() -> None:
    app = create_app(
        seed=int(os.environ.get("TRANSMITTER_SEED", "42")),
        customers=int(os.environ.get("TRANSMITTER_CUSTOMERS", "5")),
        limits=os.environ.get("TRANSMITTER_LIMITS"),
    )
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")


if __name__ == "__main__":
    main()
