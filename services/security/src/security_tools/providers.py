"""Keycloak's provider jar of ours: the script mapper that approves a consent at the
transmitter (infra/security/authorise-consent.js), deployed as Keycloak's `scripts` feature
wants it: the script and META-INF/keycloak-scripts.json in a jar in /opt/keycloak/providers.
No Java: a jar is a zip. Written with fixed timestamps, so the same script gives the same jar
(and the pod rolls only when it changes)."""

import json
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[4] / "infra/security/authorise-consent.js"
FILE_NAME = "authorise-consent.js"
# Keycloak names a deployed script mapper `script-<file name>`.
MAPPER_ID = f"script-{FILE_NAME}"
JAR = "ofar-scripts.jar"
DESCRIPTOR = {"mappers": [{"name": "OFAR authorise consent", "fileName": FILE_NAME,
                           "description": "Approves consent:<id> at the transmitter when a customer's token is issued"}]}


def build(out: Path, script: Path = SCRIPT) -> Path:
    path = out / JAR
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as jar:
        for name, data in (("META-INF/keycloak-scripts.json", json.dumps(DESCRIPTOR, indent=1)),
                           (FILE_NAME, script.read_text())):
            jar.writestr(zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0)), data)
    return path
