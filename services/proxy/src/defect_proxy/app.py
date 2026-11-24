"""The proxy: forwards every request to the transmitter, may inject one catalogued defect into
the response, and labels each Open Finance exchange (docs/defects.md).

Own endpoints, under /_proxy: healthz, run (settings and counters) and flush (write the
buffered labels now). Everything else, /mock/* included, goes to the transmitter.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx2
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .catalogue import Context, Exchange, route_of
from .labels import LabelSink
from .selection import choose

HOP_BY_HOP = {"connection", "keep-alive", "transfer-encoding", "te", "trailer", "upgrade", "proxy-authorization",
              "proxy-authenticate", "host", "content-length", "content-encoding", "accept-encoding"}


def new_run_id() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def create_app(upstream: str | httpx2.AsyncBaseTransport, seed: int, rates: dict[str, float], sink: LabelSink,
               flush_seconds: float = 5.0) -> Starlette:
    if isinstance(upstream, str):
        client = httpx2.AsyncClient(base_url=upstream, timeout=10.0)
    else:  # tests: the transmitter in process
        client = httpx2.AsyncClient(transport=upstream, base_url="http://transmitter", timeout=10.0)
    counters = {"exchanges": 0, "defects": 0}
    seeded: dict[str, str] = {}

    async def substitute_consent(token: str) -> str | None:
        """The AUTHORISED seeded consent of the customer whose consent is `token`."""
        if not seeded:
            listing = (await client.get("/mock/seed")).json()
            for customer in listing["customers"]:
                authorised = next((c for c in customer["consents"] if c.endswith("-authorised")), None)
                for consent in customer["consents"]:
                    if authorised and consent != authorised:
                        seeded[consent] = authorised
        return seeded.get(token)

    async def extensions(consent_id: str) -> list[dict]:
        """The consent's renewal history at the transmitter, newest first (one page is enough)."""
        response = await client.get(f"/open-banking/consents/v3/consents/{consent_id}/extensions",
                                    params={"page-size": "1000"},
                                    headers={"authorization": "Bearer proxy", "x-fapi-interaction-id": str(uuid.uuid4())})
        body = parse(response)
        data = body.get("data") if response.status_code == 200 and isinstance(body, dict) else None
        return data if isinstance(data, list) else []

    context = Context(substitute_consent=substitute_consent, extensions=extensions)

    async def forward(request: Request, body: bytes, authorization: str | None = None) -> httpx2.Response:
        headers = [(k, v) for k, v in request.headers.items() if k.lower() not in HOP_BY_HOP]
        # The caller's Host, so the transmitter's links (self, next, ...) point back at the proxy.
        headers.append(("host", request.headers.get("host", "")))
        if authorization is not None:
            headers = [(k, v) for k, v in headers if k.lower() != "authorization"] + [("authorization", authorization)]
        return await client.request(request.method, request.url.path, params=list(request.query_params.multi_items()),
                                    headers=headers, content=body or None)

    def response_headers(upstream_response: httpx2.Response) -> dict[str, str]:
        return {k.lower(): v for k, v in upstream_response.headers.items() if k.lower() not in HOP_BY_HOP}

    def parse(upstream_response: httpx2.Response) -> Any:
        if "json" not in upstream_response.headers.get("content-type", "") or not upstream_response.content:
            return None
        try:
            return upstream_response.json()
        except ValueError:
            return None

    async def proxy(request: Request) -> Response:
        body = await request.body()
        upstream_response = await forward(request, body)
        route = route_of(request.method, request.url.path)
        if not request.url.path.startswith("/open-banking/"):
            return Response(upstream_response.content, upstream_response.status_code, response_headers(upstream_response))

        async def replaced(token: str) -> tuple[int, dict[str, str], Any]:
            again = await forward(request, body, authorization=f"Bearer {token}")
            return again.status_code, response_headers(again), parse(again)

        ex = Exchange(
            method=request.method, path=request.url.path, route=route,
            query=list(request.query_params.multi_items()),
            request_headers={k.lower(): v for k, v in request.headers.items()},
            status=upstream_response.status_code, headers=response_headers(upstream_response),
            body=parse(upstream_response), upstream_status=upstream_response.status_code, replaced=replaced,
        )
        chosen = choose(seed, rates, ex)
        injection, defect = None, None
        if chosen is not None:
            defect, rng = chosen
            injection = await defect.inject(ex, rng, context)
            if injection is None:
                defect = None

        counters["exchanges"] += 1
        counters["defects"] += defect is not None
        record = {
            "run_id": sink.run_id,
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z"),
            "interaction_id": request.headers.get("x-fapi-interaction-id"),
            "method": request.method, "path": request.url.path, "route": route,
            "query": request.url.query or None, "consent": ex.token,
            "upstream_status": ex.upstream_status, "status": ex.status, "seed": seed,
            "defect_class": defect.id if defect else None, "defect_layer": defect.layer if defect else None,
            "defect_variant": injection.variant if injection else None,
            "defect_target": injection.target if injection else None,
            "defect_detail": injection.detail if injection else None,
        }
        if sink.add(record):
            asyncio.create_task(sink.aflush())

        if defect is None and ex.status == upstream_response.status_code:
            return Response(upstream_response.content, upstream_response.status_code, ex.headers)
        content = b"" if ex.body is None else json.dumps(ex.body, ensure_ascii=False, separators=(",", ":")).encode()
        return Response(content, ex.status, ex.headers)

    async def healthz(request: Request) -> Response:
        return JSONResponse({"status": "ok"})

    async def run(request: Request) -> Response:
        return JSONResponse({"run_id": sink.run_id, "seed": seed, "rates": rates, "labels_prefix": sink.prefix,
                             "labels_written": sink.written, **counters})

    async def flush(request: Request) -> Response:
        await sink.aflush()
        return JSONResponse({"run_id": sink.run_id, "labels_written": sink.written, "labels_prefix": sink.prefix})

    @contextlib.asynccontextmanager
    async def lifespan(app: Starlette):
        async def periodic() -> None:
            while True:
                await asyncio.sleep(flush_seconds)
                with contextlib.suppress(Exception):
                    await sink.aflush()

        task = asyncio.create_task(periodic())
        try:
            yield
        finally:
            task.cancel()
            await sink.aflush()
            await client.aclose()

    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    return Starlette(routes=[
        Route("/_proxy/healthz", healthz),
        Route("/_proxy/run", run),
        Route("/_proxy/flush", flush, methods=["POST"]),
        Route("/{path:path}", proxy, methods=methods),
    ], lifespan=lifespan)
