"""HTTP layer: Consents 3.3.1 and Accounts 2.4.2 over the seeded dataset.

Through the mTLS gateway the transmitter is a FAPI-BR resource server (security.py): the
consents endpoints take a client_credentials token with scope `consents` and see only the
consents of that client; the accounts endpoints and the renewal take a token whose
`consent:<id>` names an AUTHORISED consent of the same client.

Mock-only stand-ins, outside the specifications (docs/transmitter.md), for requests that do not
come through the gateway:
- the accounts endpoints and the consent renewal (POST /consents/{consentId}/extends) take
  `Authorization: Bearer <consentId>` instead of a FAPI access or authorization_code token bound
  to the consent; the other consents endpoints accept any bearer token;
- POST /mock/consents/{consentId}/authorise stands for the customer's approval at the
  transmitter (the authorisation server calls it too, with the customer's CPF); GET /mock/seed
  lists the seeded customers, consents and accounts.
"""

from __future__ import annotations

import json
import math
import re
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlencode

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import dataset as ds
from .consents import MAX_VALIDITY, Consent, ConsentStore, Extension, seed_consents
from .limits import Limits, configured, follow_up_page, operation
from .security import CERT_HEADER, GATEWAY_HEADER, Caller, Refused, Verifier, certificate, organisation_of, unverified_consent

CONSENTS = "/open-banking/consents/v3"
ACCOUNTS = "/open-banking/accounts/v2"
VERSIONS = {"consents": "3.3.1", "accounts": "2.4.2"}

JSON = "application/json"
JSON_ERROR = "application/json; charset=utf-8"

UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
DATETIME_RE = re.compile(
    r"^(\d{4})-(1[0-2]|0?[1-9])-(3[01]|[12][0-9]|0?[1-9])T(?:[01]\d|2[0123]):(?:[012345]\d):(?:[012345]\d)Z$"
)
PERMISSIONS = {
    "ACCOUNTS_READ", "ACCOUNTS_BALANCES_READ", "ACCOUNTS_TRANSACTIONS_READ", "ACCOUNTS_OVERDRAFT_LIMITS_READ",
    "CREDIT_CARDS_ACCOUNTS_READ", "CREDIT_CARDS_ACCOUNTS_BILLS_READ", "CREDIT_CARDS_ACCOUNTS_BILLS_TRANSACTIONS_READ",
    "CREDIT_CARDS_ACCOUNTS_LIMITS_READ", "CREDIT_CARDS_ACCOUNTS_TRANSACTIONS_READ",
    "CUSTOMERS_PERSONAL_IDENTIFICATIONS_READ", "CUSTOMERS_PERSONAL_ADITTIONALINFO_READ",
    "CUSTOMERS_BUSINESS_IDENTIFICATIONS_READ", "CUSTOMERS_BUSINESS_ADITTIONALINFO_READ",
    "FINANCINGS_READ", "FINANCINGS_SCHEDULED_INSTALMENTS_READ", "FINANCINGS_PAYMENTS_READ", "FINANCINGS_WARRANTIES_READ",
    "INVOICE_FINANCINGS_READ", "INVOICE_FINANCINGS_SCHEDULED_INSTALMENTS_READ", "INVOICE_FINANCINGS_PAYMENTS_READ",
    "INVOICE_FINANCINGS_WARRANTIES_READ", "LOANS_READ", "LOANS_SCHEDULED_INSTALMENTS_READ", "LOANS_PAYMENTS_READ",
    "LOANS_WARRANTIES_READ", "UNARRANGED_ACCOUNTS_OVERDRAFT_READ", "UNARRANGED_ACCOUNTS_OVERDRAFT_SCHEDULED_INSTALMENTS_READ",
    "UNARRANGED_ACCOUNTS_OVERDRAFT_PAYMENTS_READ", "UNARRANGED_ACCOUNTS_OVERDRAFT_WARRANTIES_READ", "RESOURCES_READ",
    "BANK_FIXED_INCOMES_READ", "CREDIT_FIXED_INCOMES_READ", "FUNDS_READ", "VARIABLE_INCOMES_READ",
    "TREASURE_TITLES_READ", "EXCHANGES_READ",
}
ACCOUNT_TYPES = {"CONTA_DEPOSITO_A_VISTA", "CONTA_POUPANCA", "CONTA_PAGAMENTO_PRE_PAGA"}
MIN_PAGE_SIZE, MAX_PAGE_SIZE = 25, 1000
# Stand-in for the organisationId of the caller's certificate (the origin of the limits).
ORIGIN_HEADER = "x-mock-client-org-id"
DEFAULT_RECEIVER = str(uuid.uuid5(uuid.NAMESPACE_DNS, "receiver.ofb.svc"))


class ApiError(Exception):
    def __init__(self, status: int, code: str, title: str, detail: str) -> None:
        super().__init__(detail)
        self.status, self.code, self.title, self.detail = status, code, title, detail


def fmt(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_ms(moment: datetime) -> str:
    moment = moment.astimezone(timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def money(amount) -> dict[str, str]:
    return {"amount": f"{amount:.2f}", "currency": ds.CURRENCY}


def api_of(path: str) -> str | None:
    if path.startswith(CONSENTS):
        return "consents"
    if path.startswith(ACCOUNTS):
        return "accounts"
    return None


def create_app(seed: int = 42, customers: int = 5,
               clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
               limits: str | None = None, verifier: Verifier | None = None,
               seed_client: str | None = None) -> FastAPI:
    """`limits`: TRANSMITTER_LIMITS, JSON raising some limits above the minimums (limits.py).
    `verifier`: checks the tokens of requests through the gateway (security.py); without one,
    those requests are refused. `seed_client`: the client the seeded consents belong to (the
    walk's, TRANSMITTER_SEED_CLIENT)."""
    started = clock()
    data = ds.build(seed, started.date(), customers)
    store = ConsentStore()
    for consent in seed_consents(data, started, seed_client):
        store.add(consent)

    app = FastAPI(title="Synthetic Open Finance transmitter", docs_url=None, redoc_url=None, openapi_url=None)
    counters = Limits(configured(limits))
    app.state.dataset, app.state.store, app.state.limits = data, store, counters

    # --- plumbing -------------------------------------------------------------------------

    def interaction_id(request: Request) -> str:
        """The caller's x-fapi-interaction-id; a missing or invalid one is a 400 (with a new id)."""
        value = request.headers.get("x-fapi-interaction-id", "")
        if UUID_RE.match(value):
            request.state.interaction_id = value
            return value
        request.state.interaction_id = str(uuid.uuid4())
        raise ApiError(400, "PARAMETRO_INVALIDO", "x-fapi-interaction-id inválido",
                       "O cabeçalho x-fapi-interaction-id é obrigatório e deve ser um UUID RFC4122.")

    def headers(request: Request) -> dict[str, str]:
        out = {"x-fapi-interaction-id": getattr(request.state, "interaction_id", None) or str(uuid.uuid4())}
        api = api_of(request.url.path)
        if api:
            out["x-v"] = VERSIONS[api]
        return out

    def ok(request: Request, body: dict, status: int = 200) -> JSONResponse:
        return JSONResponse(body, status_code=status, headers=headers(request), media_type=JSON)

    def error(request: Request, status: int, code: str, title: str, detail: str) -> JSONResponse:
        meta: dict = {"requestDateTime": fmt(clock())}
        if api_of(request.url.path) == "accounts":
            meta = {"totalRecords": 1, "totalPages": 1, **meta}
        body = {"errors": [{"code": code, "title": title, "detail": detail}], "meta": meta}
        return JSONResponse(body, status_code=status, headers=headers(request), media_type=JSON_ERROR)

    @app.exception_handler(ApiError)
    async def on_api_error(request: Request, exc: ApiError):
        return error(request, exc.status, exc.code, exc.title, exc.detail)

    @app.exception_handler(StarletteHTTPException)
    async def on_http_error(request: Request, exc: StarletteHTTPException):
        value = request.headers.get("x-fapi-interaction-id", "")
        request.state.interaction_id = value if UUID_RE.match(value) else str(uuid.uuid4())
        titles = {404: ("NAO_ENCONTRADO", "Recurso não encontrado"), 405: ("METODO_NAO_PERMITIDO", "Método não permitido")}
        code, title = titles.get(exc.status_code, ("ERRO_NAO_MAPEADO", "Erro"))
        return error(request, exc.status_code, code, title, str(exc.detail))

    @app.middleware("http")
    async def apply_limits(request: Request, call_next):
        """Traffic limit first (429), then the monthly one (423); see limits.py."""
        endpoint = operation(request.method, request.url.path)
        if endpoint is None:
            return await call_next(request)
        value = request.headers.get("x-fapi-interaction-id", "")
        request.state.interaction_id = value if UUID_RE.match(value) else str(uuid.uuid4())
        auth = request.headers.get("authorization", "")
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        if via_gateway(request):
            # The organisation of the certificate; the consent of the token, unchecked (only a
            # successful response, so a checked token, is counted).
            try:
                origin = organisation_of(certificate(request.headers.get(CERT_HEADER)))
            except Refused:
                origin = DEFAULT_RECEIVER
            consent_key = unverified_consent(bearer) if bearer else None
        else:
            origin = request.headers.get(ORIGIN_HEADER) or DEFAULT_RECEIVER
            consent_key = bearer or None
        now = clock()
        if not counters.admit_traffic(endpoint, origin, now):
            return error(request, 429, "LIMITE_TRAFEGO_EXCEDIDO", "Limite de tráfego excedido",
                         "O limite de requisições por minuto deste endpoint para a instituição foi atingido.")
        key = None
        if counters.limits[endpoint][1] is not None and not follow_up_page(dict(request.query_params)):
            consent = store.get(consent_key, now) if consent_key else None
            if consent is not None and consent.status == "AUTHORISED":
                key = counters.monthly_key(endpoint, request.url.path, consent.consent_id, consent.cpf, origin, now)
            if key is not None and counters.monthly_exhausted(key):
                return error(request, 423, "LIMITE_OPERACIONAL_EXCEDIDO", "Limite operacional excedido",
                             "O limite mensal de chamadas deste endpoint para o recurso foi atingido.")
        response = await call_next(request)
        if key is not None and 200 <= response.status_code < 300:
            counters.count_monthly(key)
        return response

    def require_bearer(request: Request) -> str:
        value = request.headers.get("authorization", "")
        if not value.lower().startswith("bearer ") or not value[7:].strip():
            raise ApiError(401, "NAO_AUTORIZADO", "Não autorizado", "Cabeçalho Authorization com token Bearer ausente.")
        return value[7:].strip()

    def via_gateway(request: Request) -> bool:
        return GATEWAY_HEADER in request.headers

    def fapi_caller(request: Request, scope: str) -> Caller | None:
        """The checked caller of a request through the gateway (None for the stand-ins). A
        token that does not check is a 401; one without the scope the endpoint needs, a 403."""
        if not via_gateway(request):
            return None
        token = require_bearer(request)
        if verifier is None:
            raise ApiError(401, "NAO_AUTORIZADO", "Não autorizado", "O servidor de recursos não está configurado.")
        try:
            caller = verifier.check(token, request.headers.get(CERT_HEADER))
        except Refused as exc:
            raise ApiError(401, "NAO_AUTORIZADO", "Não autorizado", f"Token recusado: {exc}.") from None
        if scope not in caller.scopes:
            raise ApiError(403, "PERMISSAO_INSUFICIENTE", "Escopo insuficiente", f"O token não tem o escopo {scope}.")
        return caller

    foreign = verifier is not None and "foreign-consent" in verifier.defects

    def owned(consent: Consent, caller: Caller | None) -> Consent:
        """Through the gateway a client sees only the consents it created (the defect
        foreign-consent skips that)."""
        if caller is not None and consent.client_id != caller.client_id and not foreign:
            raise ApiError(403, "NAO_AUTORIZADO", "Consentimento de outro cliente",
                           "O consentimento não pertence ao cliente do token.")
        return consent

    def self_link(request: Request) -> str:
        return str(request.url)

    # --- consents -------------------------------------------------------------------------

    def consent_body(request: Request, consent: Consent, read: bool) -> dict:
        body: dict = {
            "consentId": consent.consent_id,
            "creationDateTime": fmt(consent.created),
            "status": consent.status,
            "statusUpdateDateTime": fmt(consent.status_updated),
            "permissions": consent.permissions,
        }
        if consent.expiration is not None:
            body["expirationDateTime"] = fmt(consent.expiration)
        if read and consent.rejection is not None:
            body["rejection"] = {"rejectedBy": consent.rejection.rejected_by, "reason": {"code": consent.rejection.code}}
        if read and consent.linked is not None:
            body["journey"] = {"isLinked": consent.linked}
        base = f"{request.url.scheme}://{request.url.netloc}{CONSENTS}/consents/{consent.consent_id}"
        return {"data": body, "links": {"self": base}, "meta": {"requestDateTime": fmt(clock())}}

    def bad_request(detail: str) -> ApiError:
        return ApiError(400, "PARAMETRO_INVALIDO", "Requisição malformada", detail)

    def parse_consent_request(payload) -> tuple[str, str | None, list[str], datetime | None, bool | None]:
        try:
            body = payload["data"]
            user = body["loggedUser"]["document"]
            cpf, rel = user["identification"], user["rel"]
        except (KeyError, TypeError):
            raise bad_request("data.loggedUser.document.identification e rel são obrigatórios.") from None
        if not (isinstance(cpf, str) and re.fullmatch(r"\d{11}", cpf)) or not (isinstance(rel, str) and re.fullmatch(r"[A-Z]{3}", rel)):
            raise bad_request("loggedUser.document fora do formato.")
        cnpj = None
        if "businessEntity" in body:
            try:
                entity = body["businessEntity"]["document"]
                cnpj = entity["identification"]
                if not re.fullmatch(r"[0-9A-Z]{12}[0-9]{2}", cnpj) or not re.fullmatch(r"[A-Z]{4}", entity["rel"]):
                    raise bad_request("businessEntity.document fora do formato.")
            except (KeyError, TypeError):
                raise bad_request("businessEntity.document fora do formato.") from None
        permissions = body.get("permissions")
        if not isinstance(permissions, list) or not permissions or not all(p in PERMISSIONS for p in permissions):
            raise bad_request("permissions deve ser uma lista não vazia de permissões válidas.")
        if len(set(permissions)) != len(permissions):
            raise bad_request("permissions não deve conter itens duplicados.")
        expiration = None
        if "expirationDateTime" in body:
            value = body["expirationDateTime"]
            if not isinstance(value, str) or not DATETIME_RE.match(value):
                raise bad_request("expirationDateTime fora do formato.")
            expiration = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        linked = body.get("isLinked")
        if linked is not None and not isinstance(linked, bool):
            raise bad_request("isLinked deve ser booleano.")
        return cpf, cnpj, permissions, expiration, linked

    @app.post(CONSENTS + "/consents")
    async def post_consent(request: Request):
        interaction_id(request)
        require_bearer(request)
        caller = fapi_caller(request, "consents")
        try:
            payload = await request.json()
        except ValueError:
            raise bad_request("Corpo da requisição não é JSON válido.") from None
        cpf, cnpj, permissions, expiration, linked = parse_consent_request(payload)
        now = clock()
        if expiration is not None and not (now < expiration <= now + MAX_VALIDITY):
            raise ApiError(422, "DATA_EXPIRACAO_INVALIDA", "Data de expiração inválida",
                           "expirationDateTime deve estar no futuro e em até 12 meses.")
        consent = store.create(cpf, cnpj, permissions, expiration, linked, now,
                               client_id=caller.client_id if caller else None)
        return ok(request, consent_body(request, consent, read=False), status=201)

    def find_consent(consent_id: str) -> Consent:
        consent = store.get(consent_id, clock())
        if consent is None:
            raise ApiError(404, "NAO_ENCONTRADO", "Consentimento não encontrado", f"consentId {consent_id} não existe.")
        return consent

    @app.get(CONSENTS + "/consents/{consent_id}")
    def get_consent(consent_id: str, request: Request):
        interaction_id(request)
        require_bearer(request)
        caller = fapi_caller(request, "consents")
        return ok(request, consent_body(request, owned(find_consent(consent_id), caller), read=True))

    @app.delete(CONSENTS + "/consents/{consent_id}")
    def delete_consent(consent_id: str, request: Request):
        interaction_id(request)
        require_bearer(request)
        consent = owned(find_consent(consent_id), fapi_caller(request, "consents"))
        with store.lock:
            if consent.status == "REJECTED":
                raise ApiError(422, "CONSENTIMENTO_EM_STATUS_REJEITADO", "Consentimento rejeitado",
                               "O consentimento já está em status REJECTED.")
            code = "CUSTOMER_MANUALLY_REVOKED" if consent.status == "AUTHORISED" else "CUSTOMER_MANUALLY_REJECTED"
            consent.reject(clock(), "USER", code)
        return Response(status_code=204, headers=headers(request))

    # --- accounts -------------------------------------------------------------------------

    def authorised_consent(request: Request, permission: str) -> Consent:
        token = require_bearer(request)
        caller = fapi_caller(request, "accounts")
        # Through the gateway, the consent of the token's consent:<id>, and only the client's own.
        consent_id = caller.consent_id if caller else token
        consent = store.get(consent_id, clock()) if consent_id else None
        if consent is not None and caller is not None and consent.client_id != caller.client_id and not foreign:
            consent = None
        if consent is None or consent.status != "AUTHORISED":
            raise ApiError(401, "NAO_AUTORIZADO", "Consentimento inválido",
                           "O token não corresponde a um consentimento com status AUTHORISED.")
        if permission not in consent.permissions:
            raise ApiError(403, "PERMISSAO_INSUFICIENTE", "Permissão insuficiente",
                           f"O consentimento não inclui a permissão {permission}.")
        return consent

    def find_account(consent: Consent, account_id: str) -> ds.Account:
        account = data.accounts.get(account_id)
        if account is None or account.customer_cpf != consent.cpf:
            raise ApiError(404, "NAO_ENCONTRADO", "Conta não encontrada",
                           f"accountId {account_id} não pertence ao consentimento.")
        return account

    def int_param(request: Request, name: str, default: int, low: int, high: int) -> int:
        raw = request.query_params.get(name)
        if raw is None:
            return default
        if not re.fullmatch(r"\d+", raw) or not low <= int(raw) <= high:
            raise ApiError(400, "PARAMETRO_INVALIDO", "Parâmetro inválido", f"{name} deve ser inteiro entre {low} e {high}.")
        return int(raw)

    def paginate(request: Request, items: list, with_last: bool = True) -> tuple[list, dict, int, int]:
        page = int_param(request, "page", 1, 1, 2147483647)
        size = max(int_param(request, "page-size", MIN_PAGE_SIZE, 1, MAX_PAGE_SIZE), MIN_PAGE_SIZE)
        total = len(items)
        pages = max(1, math.ceil(total / size))
        base = f"{request.url.scheme}://{request.url.netloc}{request.url.path}"
        other = [(k, v) for k, v in request.query_params.multi_items() if k not in ("page", "page-size")]

        def link(n: int) -> str:
            return base + "?" + urlencode(other + [("page", n), ("page-size", size)])

        links = {"self": self_link(request), "first": link(1)}
        if page > 1:
            links["prev"] = link(min(page - 1, pages))
        if page < pages:
            links["next"] = link(page + 1)
        if with_last:
            links["last"] = link(pages)
        return items[(page - 1) * size: page * size], links, total, pages

    def single(request: Request, body: dict) -> JSONResponse:
        meta = {"totalRecords": 1, "totalPages": 1, "requestDateTime": fmt(clock())}
        return ok(request, {"data": body, "links": {"self": self_link(request)}, "meta": meta})

    # --- consent extensions ----------------------------------------------------------------

    def required_header(request: Request, name: str, limit: int) -> str:
        value = request.headers.get(name, "")
        if not value or value != value.strip() or len(value) > limit:
            raise bad_request(f"O cabeçalho {name} é obrigatório.")
        return value

    @app.post(CONSENTS + "/consents/{consent_id}/extends")
    async def extend_consent(consent_id: str, request: Request):
        """Renewal by the logged user (authorization_code token: here the consent id, as on the
        accounts endpoints). Only an AUTHORISED consent, only to a later date within 12 months,
        or to no date (indeterminate)."""
        interaction_id(request)
        token = require_bearer(request)
        caller = fapi_caller(request, f"consent:{consent_id}")
        if caller is None and token != consent_id:
            raise ApiError(401, "NAO_AUTORIZADO", "Não autorizado", "O token não corresponde ao consentimento.")
        ip_address = required_header(request, "x-fapi-customer-ip-address", 100)
        user_agent = required_header(request, "x-customer-user-agent", 255)
        try:
            body = (await request.json())["data"]
            user = body["loggedUser"]["document"]
            cpf, rel = user["identification"], user["rel"]
        except (ValueError, KeyError, TypeError):
            raise bad_request("data.loggedUser.document.identification e rel são obrigatórios.") from None
        if not (isinstance(cpf, str) and re.fullmatch(r"\d{11}", cpf)) or not (isinstance(rel, str) and re.fullmatch(r"[A-Z]{3}", rel)):
            raise bad_request("loggedUser.document fora do formato.")
        expiration = None
        if "expirationDateTime" in body:
            value = body["expirationDateTime"]
            if not isinstance(value, str) or not DATETIME_RE.match(value):
                raise bad_request("expirationDateTime fora do formato.")
            expiration = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        consent = owned(find_consent(consent_id), caller)
        now = clock()
        with store.lock:
            if cpf != consent.cpf:
                raise ApiError(403, "NAO_AUTORIZADO", "Usuário não autorizado",
                               "Apenas o usuário que criou o consentimento pode renová-lo.")
            if consent.status != "AUTHORISED":
                raise ApiError(422, "ESTADO_CONSENTIMENTO_INVALIDO", "Estado inválido do consentimento",
                               f"O consentimento está em status {consent.status}.")
            current = consent.expiration
            if expiration is not None and not (now < expiration <= now + MAX_VALIDITY
                                               and (current is None or expiration > current)):
                raise ApiError(422, "DATA_EXPIRACAO_INVALIDA", "Data de expiração inválida",
                               "A nova data deve ser posterior à vigente e estar em até 12 meses.")
            consent.extensions.append(Extension(now, expiration, current, cpf, ip_address, user_agent))
            consent.expiration = expiration
        return ok(request, consent_body(request, consent, read=False), status=201)

    @app.get(CONSENTS + "/consents/{consent_id}/extensions")
    def list_extensions(consent_id: str, request: Request):
        interaction_id(request)
        require_bearer(request)
        consent = owned(find_consent(consent_id), fapi_caller(request, "consents"))
        rows = []
        for ext in reversed(consent.extensions):
            row = {"loggedUser": {"document": {"identification": ext.cpf, "rel": "CPF"}},
                   "requestDateTime": fmt(ext.requested),
                   "xFapiCustomerIpAddress": ext.ip_address, "xCustomerUserAgent": ext.user_agent}
            if ext.expiration is not None:
                row["expirationDateTime"] = fmt(ext.expiration)
            if ext.previous is not None:
                row["previousExpirationDateTime"] = fmt(ext.previous)
            rows.append(row)
        page, links, total, pages = paginate(request, rows)
        meta = {"totalRecords": total, "totalPages": pages, "requestDateTime": fmt(clock())}
        return ok(request, {"data": page, "links": links, "meta": meta})

    @app.get(ACCOUNTS + "/accounts")
    def list_accounts(request: Request):
        interaction_id(request)
        consent = authorised_consent(request, "ACCOUNTS_READ")
        account_type = request.query_params.get("accountType")
        if account_type is not None and account_type not in ACCOUNT_TYPES:
            raise ApiError(400, "PARAMETRO_INVALIDO", "Parâmetro inválido", "accountType inválido.")
        accounts = [a for a in data.accounts_of(consent.cpf) if account_type in (None, a.type)]
        rows = [{
            "brandName": ds.BRAND_NAME, "companyCnpj": data.company_cnpj, "type": a.type,
            "compeCode": ds.COMPE_CODE, "branchCode": a.branch_code, "number": a.number,
            "checkDigit": a.check_digit, "accountId": a.account_id,
        } for a in accounts]
        page, links, total, pages = paginate(request, rows)
        meta = {"totalRecords": total, "totalPages": pages, "requestDateTime": fmt(clock())}
        return ok(request, {"data": page, "links": links, "meta": meta})

    @app.get(ACCOUNTS + "/accounts/{account_id}")
    def get_account(account_id: str, request: Request):
        interaction_id(request)
        account = find_account(authorised_consent(request, "ACCOUNTS_READ"), account_id)
        return single(request, {
            "compeCode": ds.COMPE_CODE, "branchCode": account.branch_code, "number": account.number,
            "checkDigit": account.check_digit, "type": account.type, "subtype": account.subtype,
            "currency": ds.CURRENCY,
        })

    @app.get(ACCOUNTS + "/accounts/{account_id}/balances")
    def get_balances(account_id: str, request: Request):
        interaction_id(request)
        account = find_account(authorised_consent(request, "ACCOUNTS_BALANCES_READ"), account_id)
        return single(request, {
            "availableAmount": money(account.available_amount),
            "blockedAmount": money(0),
            "automaticallyInvestedAmount": money(0),
            "updateDateTime": fmt(account.last_update),
        })

    @app.get(ACCOUNTS + "/accounts/{account_id}/overdraft-limits")
    def get_overdraft_limits(account_id: str, request: Request):
        interaction_id(request)
        account = find_account(authorised_consent(request, "ACCOUNTS_OVERDRAFT_LIMITS_READ"), account_id)
        if account.overdraft_limit is None:
            return single(request, {})
        return single(request, {
            "overdraftContractedLimit": money(account.overdraft_limit),
            "overdraftUsedLimit": money(0),
            "unarrangedOverdraftAmount": money(0),
        })

    def booking_window(request: Request, past: timedelta) -> tuple[date, date]:
        today = clock().date()
        raw_from, raw_to = request.query_params.get("fromBookingDate"), request.query_params.get("toBookingDate")
        if raw_to is not None and raw_from is None:
            raise ApiError(422, "PARAMETRO_INVALIDO", "Parâmetro inválido",
                           "fromBookingDate é obrigatório quando toBookingDate é informado.")
        try:
            start = date.fromisoformat(raw_from) if raw_from else today
            end = date.fromisoformat(raw_to) if raw_to else today
        except ValueError:
            raise ApiError(400, "PARAMETRO_INVALIDO", "Parâmetro inválido", "Datas devem estar no formato AAAA-MM-DD.") from None
        if start > end or start < today - past or end > today + timedelta(days=365):
            raise ApiError(422, "PARAMETRO_INVALIDO", "Período inválido",
                           "Período fora do permitido para o endpoint ou com data inicial após a final.")
        return start, end

    def transactions(request: Request, account_id: str, past: timedelta) -> JSONResponse:
        interaction_id(request)
        account = find_account(authorised_consent(request, "ACCOUNTS_TRANSACTIONS_READ"), account_id)
        start, end = booking_window(request, past)
        indicator = request.query_params.get("creditDebitIndicator")
        if indicator is not None and indicator not in ("CREDITO", "DEBITO"):
            raise ApiError(400, "PARAMETRO_INVALIDO", "Parâmetro inválido", "creditDebitIndicator inválido.")
        selected = [
            t for t in reversed(account.transactions)
            if start <= t.booking_date <= end and indicator in (None, t.credit_debit)
        ]
        rows = []
        for t in selected:
            row = {
                "transactionId": t.transaction_id, "completedAuthorisedPaymentType": t.status,
                "creditDebitType": t.credit_debit, "transactionName": t.name, "type": t.type,
                "transactionAmount": money(t.amount), "transactionDateTime": fmt_ms(t.timestamp),
            }
            if t.partie:
                row.update(t.partie)
            rows.append(row)
        page, links, _, _ = paginate(request, rows, with_last=False)
        return ok(request, {"data": page, "links": links, "meta": {"requestDateTime": fmt(clock())}})

    @app.get(ACCOUNTS + "/accounts/{account_id}/transactions")
    def get_transactions(account_id: str, request: Request):
        return transactions(request, account_id, timedelta(days=365))

    @app.get(ACCOUNTS + "/accounts/{account_id}/transactions-current")
    def get_transactions_current(account_id: str, request: Request):
        return transactions(request, account_id, timedelta(days=6))

    # --- mock-only ------------------------------------------------------------------------

    @app.post("/mock/consents/{consent_id}/authorise")
    async def authorise(consent_id: str, request: Request):
        """The customer's approval. The authorisation server sends the CPF of the customer who
        logged in, which must be the consent's (the walk sends none)."""
        consent = store.get(consent_id, clock())
        if consent is None:
            return JSONResponse({"error": "unknown consent"}, status_code=404)
        body = await request.body()
        try:
            cpf = json.loads(body).get("cpf") if body else None
        except (ValueError, AttributeError):
            return JSONResponse({"error": "body must be a JSON object"}, status_code=400)
        if cpf is not None and cpf != consent.cpf:
            return JSONResponse({"error": "the customer is not the consent's"}, status_code=403)
        with store.lock:
            if consent.status != "AWAITING_AUTHORISATION":
                return JSONResponse({"error": f"consent is {consent.status}"}, status_code=409)
            consent.status, consent.status_updated = "AUTHORISED", clock()
        return {"consentId": consent_id, "status": consent.status}

    @app.get("/mock/seed")
    def seed_listing():
        return {
            "seed": data.seed,
            "today": data.today.isoformat(),
            "customers": [{
                "cpf": c.cpf,
                "consents": [k for k, v in store.consents.items() if v.cpf == c.cpf and k.startswith("urn:transmitter:seed-")],
                "accounts": [a.account_id for a in data.accounts_of(c.cpf)],
            } for c in data.customers],
        }

    instance = str(uuid.uuid4())

    @app.get("/healthz")
    def healthz():
        # The instance tells a fresh pod from the one it replaced (scripts/limits-run.sh).
        return {"status": "ok", "instance": instance}

    return app
