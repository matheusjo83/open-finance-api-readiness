"""The pinned specifications: openapi-core for validation, the raw document for the strict rule
(which needs the response schema of an operation, with the local $refs resolved)."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import yaml
from openapi_core import OpenAPI

# Path prefix -> pinned file, as downloaded by `make specs`.
SPEC_FILES = {
    "/open-banking/consents/v3": "consents-3.3.1.yml",
    "/open-banking/accounts/v2": "accounts-2.4.2.yml",
}
METHODS = {"get", "put", "post", "delete", "patch", "head", "options"}


@dataclass
class Operation:
    id: str
    method: str
    template: str  # e.g. /accounts/{accountId}
    raw: dict[str, Any]


@dataclass
class Spec:
    name: str  # e.g. accounts-2.4.2
    prefix: str  # e.g. /open-banking/accounts/v2
    host_url: str  # the first server's scheme and host; requests are validated as if sent there
    openapi: OpenAPI
    raw: dict[str, Any]
    operations: list[tuple[re.Pattern[str], Operation]] = field(default_factory=list)

    @classmethod
    def load(cls, path: Path, prefix: str) -> Spec:
        with open(path, encoding="utf-8-sig") as f:  # the Consents file starts with a BOM
            raw = yaml.safe_load(f)
        server = urlsplit(raw["servers"][0]["url"])
        if server.path.rstrip("/") != prefix:
            raise ValueError(f"{path}: server path {server.path} is not {prefix}")
        spec = cls(name=path.stem, prefix=prefix, host_url=f"{server.scheme}://{server.netloc}",
                   openapi=OpenAPI.from_dict(raw), raw=raw)
        for template, item in raw["paths"].items():
            pattern = re.compile("^" + re.sub(r"\\\{[^}]+\\\}", "[^/]+", re.escape(template)) + "$")
            for method, op in item.items():
                if method in METHODS:
                    spec.operations.append((pattern, Operation(op.get("operationId", f"{method} {template}"),
                                                               method.upper(), template, op)))
        return spec

    def operation(self, method: str, path: str) -> Operation | None:
        relative = path[len(self.prefix):] or "/"
        return next((op for pattern, op in self.operations if op.method == method and pattern.match(relative)), None)

    def resolve(self, node: Any) -> Any:
        """Follows local $refs (#/components/...) until a node without one."""
        seen = set()
        while isinstance(node, dict) and "$ref" in node:
            ref = node["$ref"]
            if not ref.startswith("#/") or ref in seen:
                raise ValueError(f"{self.name}: unsupported or cyclic $ref {ref}")
            seen.add(ref)
            node = self.raw
            for part in ref[2:].split("/"):
                node = node[part.replace("~1", "/").replace("~0", "~")]
        return node

    def response_schema(self, op: Operation, status: int, content_type: str) -> dict[str, Any] | None:
        responses = op.raw.get("responses", {})
        response = responses.get(str(status)) or responses.get(f"{str(status)[0]}XX") or responses.get("default")
        if response is None:
            return None
        content = self.resolve(response).get("content", {})
        media = content_type.split(";")[0].strip().lower()
        chosen = content.get(media) or next(
            (v for k, v in content.items() if k.split(";")[0].strip().lower() == media), None)
        return None if chosen is None or "schema" not in chosen else self.resolve(chosen["schema"])


def load_specs(directory: Path) -> list[Spec]:
    missing = [name for name in SPEC_FILES.values() if not (directory / name).exists()]
    if missing:
        raise FileNotFoundError(f"{', '.join(missing)} not found in {directory}: run `make specs`")
    return [Spec.load(directory / name, prefix) for prefix, name in SPEC_FILES.items()]


def spec_for(specs: list[Spec], path: str) -> Spec | None:
    return next((s for s in specs if path == s.prefix or path.startswith(s.prefix + "/")), None)
