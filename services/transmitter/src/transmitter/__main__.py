"""Entry point: python -m transmitter (settings from the environment)."""

import os
from datetime import datetime, timezone
from pathlib import Path

import uvicorn

from .app import create_app
from .security import KeycloakClient, Settings, Verifier


def verifier() -> Verifier | None:
    """The resource server's checks, when TRANSMITTER_ISSUER is set. The Secret with its
    credentials may come after the pod (make security-deploy restarts it then)."""
    issuer = os.environ.get("TRANSMITTER_ISSUER")
    if not issuer:
        return None
    client_id = os.environ.get("TRANSMITTER_CLIENT_ID", "ofar-transmitter")
    keycloak = KeycloakClient(Settings(issuer, client_id, Path(os.environ.get("TRANSMITTER_SECURITY_DIR", "/security"))))
    return Verifier(issuer=issuer, audience=client_id, jwks=keycloak.jwks, introspect=keycloak.introspect,
                    clock=lambda: datetime.now(timezone.utc))


def main() -> None:
    app = create_app(
        seed=int(os.environ.get("TRANSMITTER_SEED", "42")),
        customers=int(os.environ.get("TRANSMITTER_CUSTOMERS", "5")),
        limits=os.environ.get("TRANSMITTER_LIMITS"),
        verifier=verifier(),
    )
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")


if __name__ == "__main__":
    main()
