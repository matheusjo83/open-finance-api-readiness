"""Contract rules applied to every exchange (docs/validator.md):

- schema.*: the response against the pinned specification, with openapi-core (status,
  required headers, content type, body).
- interaction-id.echo: the x-fapi-interaction-id the client sent comes back in the response.
  Both specs say the transmitter must echo it; only Accounts marks the header as required.
- strict.undeclared-field: a body field the response schema does not declare. No pinned schema
  sets additionalProperties: false on a success body, so schema validation accepts it.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import parse_qsl

from openapi_core.datatypes import RequestParameters
from openapi_core.templating.media_types.exceptions import MediaTypeFinderError
from openapi_core.templating.paths.exceptions import PathError
from openapi_core.templating.responses.exceptions import ResponseFinderError
from openapi_core.validation.response.exceptions import (
    DataValidationError, HeadersError, HeaderValidationError, MissingData,
)
from openapi_core.validation.schemas.exceptions import InvalidSchemaValue
from werkzeug.datastructures import Headers, ImmutableMultiDict

from .spec import Operation, Spec

ERROR, WARNING = "error", "warning"
RULES = {
    "schema.operation": ERROR,
    "schema.status": ERROR,
    "schema.content-type": ERROR,
    "schema.header": ERROR,
    "schema.body": ERROR,
    "interaction-id.echo": ERROR,
    "strict.undeclared-field": WARNING,
}


@dataclass
class Exchange:
    """One request the validator sent and the response it got."""

    method: str
    path: str
    query: str
    request_headers: dict[str, str]
    request_body: bytes | None
    status: int
    headers: dict[str, str]  # lower-case names
    body: bytes

    @property
    def interaction_id(self) -> str | None:
        return self.request_headers.get("x-fapi-interaction-id")

    @property
    def content_type(self) -> str:
        return self.headers.get("content-type", "")


@dataclass
class Finding:
    rule: str
    pointer: str  # a JSON pointer into the body, header:<name>, status or content-type
    message: str
    severity: str = ""

    def __post_init__(self) -> None:
        self.severity = self.severity or RULES[self.rule]

    def as_dict(self) -> dict[str, str]:
        return asdict(self)


class _Request:
    def __init__(self, spec: Spec, ex: Exchange) -> None:
        self.host_url, self.path, self.method = spec.host_url, ex.path, ex.method.lower()
        self.parameters = RequestParameters(query=ImmutableMultiDict(parse_qsl(ex.query)),
                                            header=Headers(list(ex.request_headers.items())))
        self.body = ex.request_body
        self.content_type = ex.request_headers.get("content-type", "")


class _Response:
    def __init__(self, ex: Exchange) -> None:
        self.status_code = ex.status
        self.headers = Headers(list(ex.headers.items()))
        self.content_type = ex.content_type
        self.data = ex.body or None


def _pointer(path) -> str:
    return "".join(f"/{str(p).replace('~', '~0').replace('/', '~1')}" for p in path)


def _schema_findings(errors) -> list[Finding]:
    out: list[Finding] = []
    for error in errors:
        if isinstance(error, PathError):
            out.append(Finding("schema.operation", "operation", str(error)))
        elif isinstance(error, ResponseFinderError):
            out.append(Finding("schema.status", "status", str(error)))
        elif isinstance(error, MediaTypeFinderError):
            out.append(Finding("schema.content-type", "content-type", str(error)))
        elif isinstance(error, HeadersError):
            out += _schema_findings(error.context)
        elif isinstance(error, HeaderValidationError):
            cause = error.__cause__
            detail = f": {cause}" if cause else ""
            out.append(Finding("schema.header", f"header:{error.name.lower()}", f"{error}{detail}"))
        elif isinstance(error, MissingData):
            out.append(Finding("schema.body", "", str(error)))
        elif isinstance(error, DataValidationError):
            cause = error.__cause__
            if isinstance(cause, InvalidSchemaValue) and cause.schema_errors:
                out += [Finding("schema.body", _pointer(e.absolute_path), e.message) for e in cause.schema_errors]
            else:
                out.append(Finding("schema.body", "", str(cause or error)))
        else:
            out.append(Finding("schema.body", "", f"{type(error).__name__}: {error}"))
    return out


def check_schema(spec: Spec, ex: Exchange) -> list[Finding]:
    return _schema_findings(spec.openapi.iter_response_errors(_Request(spec, ex), _Response(ex)))


def check_interaction_id(ex: Exchange) -> list[Finding]:
    sent, got = ex.interaction_id, ex.headers.get("x-fapi-interaction-id")
    if sent is None:  # nothing to echo: the walk always sends one
        return []
    if got is None:
        return [Finding("interaction-id.echo", "header:x-fapi-interaction-id", "not returned")]
    if got != sent:
        return [Finding("interaction-id.echo", "header:x-fapi-interaction-id", f"sent {sent}, got {got}")]
    return []


def undeclared_fields(spec: Spec, schema: dict[str, Any], value: Any, pointer: str = "") -> list[str]:
    """JSON pointers of the object members `schema` does not declare. allOf, oneOf and anyOf
    declare the union of their branches; additionalProperties (false, true or a schema) means the
    schema itself decides about extra members, so the strict rule leaves them alone."""
    schema = spec.resolve(schema)
    branches = [spec.resolve(s) for key in ("allOf", "oneOf", "anyOf") for s in schema.get(key, [])]
    if isinstance(value, dict):
        properties: dict[str, Any] = dict(schema.get("properties", {}))
        extra = schema.get("additionalProperties")
        for branch in branches:
            properties.update(branch.get("properties", {}))
            extra = branch.get("additionalProperties", extra)
        if not properties:  # a free-form object, or not an object at all (schema.body reports that)
            return []
        out = []
        for key, member in value.items():
            here = f"{pointer}/{key.replace('~', '~0').replace('/', '~1')}"
            if key in properties:
                out += undeclared_fields(spec, properties[key], member, here)
            elif isinstance(extra, dict):
                out += undeclared_fields(spec, extra, member, here)
            elif extra is None:
                out.append(here)
        return out
    if isinstance(value, list):
        items = schema.get("items") or next((b["items"] for b in branches if "items" in b), None)
        if items is None:
            return []
        return [p for i, member in enumerate(value) for p in undeclared_fields(spec, items, member, f"{pointer}/{i}")]
    return []


def check_strict(spec: Spec, op: Operation | None, ex: Exchange, document: Any) -> list[Finding]:
    if op is None or document is None:
        return []
    schema = spec.response_schema(op, ex.status, ex.content_type)
    if schema is None:
        return []
    return [Finding("strict.undeclared-field", p, "not declared in the response schema")
            for p in undeclared_fields(spec, schema, document)]


def parse_json(ex: Exchange) -> Any:
    if "json" not in ex.content_type or not ex.body:
        return None
    try:
        return json.loads(ex.body)
    except ValueError:
        return None


def check(spec: Spec, ex: Exchange) -> tuple[Operation | None, list[Finding]]:
    op = spec.operation(ex.method, ex.path)
    findings = check_schema(spec, ex) + check_interaction_id(ex) + check_strict(spec, op, ex, parse_json(ex))
    return op, findings
