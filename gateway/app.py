"""Demo FHIR gateway standing in for the EHR's API front door.

It does the parts of an EHR's FHIR endpoint that the pipeline depends on, so the client code is
exercised for real:

* SMART Backend Services token endpoint: verifies the client's signed JWT assertion (RS384, audience,
  expiry, one-time jti) against the client's registered public key and issues short-lived tokens.
* Scope enforcement per resource type (system/<Type>.read) on every request.
* Read-only: anything other than GET is rejected.
* Resource hiding: types listed in HIDDEN_RESOURCES are removed from the CapabilityStatement and
  return 404, to simulate a tenant that doesn't expose them (by default, Appointment).

Behind it sits a stock HAPI FHIR server with synthetic data.
"""

from __future__ import annotations

import logging
import os
import secrets
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import jwt
from fastapi import FastAPI, Form, Request
from fastapi.responses import JSONResponse, Response

log = logging.getLogger("gateway")
logging.basicConfig(level=logging.INFO, format="%(asctime)s gateway %(message)s")

ASSERTION_TYPE = "urn:ietf:params:oauth:client-assertion-type:jwt-bearer"
FHIR_JSON = "application/fhir+json"
MAX_ASSERTION_LIFETIME = 300


@dataclass
class GatewaySettings:
    upstream: str = field(default_factory=lambda: os.environ.get("UPSTREAM_FHIR", "http://fhir:8080/fhir"))
    public_base: str = field(default_factory=lambda: os.environ.get("PUBLIC_BASE_URL", "http://gateway:8000"))
    client_id: str = field(default_factory=lambda: os.environ.get("CLIENT_ID", "recall-pipeline"))
    client_public_key_path: Path = field(
        default_factory=lambda: Path(os.environ.get("CLIENT_PUBLIC_KEY_PATH", "/keys/client_public.pem")))
    allowed_scopes: tuple[str, ...] = field(default_factory=lambda: tuple(os.environ.get(
        "ALLOWED_SCOPES",
        "system/Patient.read system/Practitioner.read system/Encounter.read system/Procedure.read "
        "system/MedicationAdministration.read system/Condition.read system/DocumentReference.read "
        "system/Appointment.read").split()))
    hidden_resources: frozenset[str] = field(default_factory=lambda: frozenset(
        r.strip() for r in os.environ.get("HIDDEN_RESOURCES", "Appointment").split(",") if r.strip()))
    token_ttl: int = field(default_factory=lambda: int(os.environ.get("TOKEN_TTL_SECONDS", "300")))
    signing_secret: str = field(default_factory=lambda: os.environ.get("TOKEN_SIGNING_SECRET")
                                or secrets.token_urlsafe(32))

    @property
    def token_url(self) -> str:
        return f"{self.public_base}/auth/token"


def _outcome(status: int, code: str, text: str, headers: dict | None = None) -> JSONResponse:
    body = {"resourceType": "OperationOutcome",
            "issue": [{"severity": "error", "code": code, "diagnostics": text}]}
    return JSONResponse(body, status_code=status, media_type=FHIR_JSON, headers=headers)


def _oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": error, "error_description": description}, status_code=status)


def create_app(settings: GatewaySettings | None = None, upstream_transport: httpx.BaseTransport | None = None,
               clock=time.time) -> FastAPI:
    settings = settings or GatewaySettings()
    upstream = httpx.Client(transport=upstream_transport, timeout=60)
    seen_jti: dict[str, float] = {}
    app = FastAPI(title="Demo FHIR gateway", docs_url=None, redoc_url=None)
    app.state.settings = settings

    def client_public_key() -> bytes | None:
        try:
            return settings.client_public_key_path.read_bytes()
        except OSError:
            return None

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/.well-known/smart-configuration")
    @app.get("/fhir/.well-known/smart-configuration")
    def smart_configuration():
        return {
            "token_endpoint": settings.token_url,
            "grant_types_supported": ["client_credentials"],
            "token_endpoint_auth_methods_supported": ["private_key_jwt"],
            "token_endpoint_auth_signing_alg_values_supported": ["RS384"],
            "scopes_supported": list(settings.allowed_scopes),
        }

    @app.post("/auth/token")
    def token(grant_type: str = Form(""), scope: str = Form(""), client_assertion_type: str = Form(""),
              client_assertion: str = Form("")):
        if grant_type != "client_credentials":
            return _oauth_error("unsupported_grant_type", "only client_credentials is supported")
        if client_assertion_type != ASSERTION_TYPE or not client_assertion:
            return _oauth_error("invalid_client", "a private_key_jwt client assertion is required", 401)
        public_key = client_public_key()
        if public_key is None:
            return _oauth_error("invalid_client", "no key registered for this client", 401)
        now = clock()
        try:
            claims = jwt.decode(client_assertion, public_key, algorithms=["RS384"], audience=settings.token_url,
                                options={"require": ["iss", "sub", "aud", "exp", "jti"]}, leeway=5)
        except jwt.PyJWTError as exc:
            log.info("token rejected: %s", exc)
            return _oauth_error("invalid_client", f"client assertion rejected: {exc}", 401)
        if claims["iss"] != settings.client_id or claims["sub"] != settings.client_id:
            return _oauth_error("invalid_client", "iss and sub must be the registered client id", 401)
        if claims["exp"] - now > MAX_ASSERTION_LIFETIME + 5:
            return _oauth_error("invalid_client", "client assertion lifetime exceeds 5 minutes", 401)
        for jti, exp in list(seen_jti.items()):
            if exp < now:
                del seen_jti[jti]
        if claims["jti"] in seen_jti:
            return _oauth_error("invalid_client", "client assertion was already used", 401)
        seen_jti[claims["jti"]] = claims["exp"]

        requested = scope.split()
        granted = [s for s in requested if s in settings.allowed_scopes]
        if not granted:
            return _oauth_error("invalid_scope", "none of the requested scopes are allowed for this client")
        access = jwt.encode({"iss": settings.public_base, "sub": claims["iss"], "scope": " ".join(granted),
                             "iat": int(now), "exp": int(now) + settings.token_ttl, "jti": str(uuid.uuid4())},
                            settings.signing_secret, algorithm="HS256")
        log.info("token issued to %s for %d scopes, ttl %ss", claims["iss"], len(granted), settings.token_ttl)
        return {"access_token": access, "token_type": "bearer", "expires_in": settings.token_ttl,
                "scope": " ".join(granted)}

    def authorize(request: Request, resource_type: str | None) -> dict | JSONResponse:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return _outcome(401, "login", "bearer token required", {"WWW-Authenticate": "Bearer"})
        try:
            claims = jwt.decode(header[7:], settings.signing_secret, algorithms=["HS256"],
                                options={"require": ["exp", "scope"]})
        except jwt.ExpiredSignatureError:
            return _outcome(401, "expired", "access token expired", {"WWW-Authenticate": "Bearer"})
        except jwt.PyJWTError:
            return _outcome(401, "login", "invalid access token", {"WWW-Authenticate": "Bearer"})
        if resource_type:
            scopes = set(claims["scope"].split())
            if not ({f"system/{resource_type}.read", f"system/{resource_type}.rs", "system/*.read"} & scopes):
                return _outcome(403, "forbidden", f"token does not grant read access to {resource_type}")
        return claims

    def forward(path: str, request: Request) -> Response:
        upstream_response = upstream.get(f"{settings.upstream}{path}", params=list(request.query_params.multi_items()),
                                         headers={"Accept": FHIR_JSON})
        return Response(content=upstream_response.content, status_code=upstream_response.status_code,
                        media_type=FHIR_JSON)

    @app.get("/fhir/metadata")
    def metadata():
        response = upstream.get(f"{settings.upstream}/metadata", headers={"Accept": FHIR_JSON})
        if response.status_code != 200:
            return _outcome(502, "transient", "upstream FHIR server unavailable")
        cap = response.json()
        for rest in cap.get("rest") or []:
            rest["resource"] = [r for r in rest.get("resource") or [] if r.get("type") not in settings.hidden_resources]
            rest["security"] = {
                "service": [{"coding": [{"system": "http://terminology.hl7.org/CodeSystem/restful-security-service",
                                         "code": "SMART-on-FHIR"}]}],
                "extension": [{"url": "http://fhir-registry.smarthealthit.org/StructureDefinition/oauth-uris",
                               "extension": [{"url": "token", "valueUri": settings.token_url}]}],
            }
        return JSONResponse(cap, media_type=FHIR_JSON)

    @app.get("/fhir")
    def paging(request: Request):
        # HAPI's next-page links point at the server root with _getpages; the page id was produced by a
        # search that was already scope-checked, so a valid token is enough here.
        if "_getpages" not in request.query_params:
            return _outcome(400, "not-supported", "system-level search is not available")
        claims = authorize(request, None)
        if isinstance(claims, JSONResponse):
            return claims
        return forward("", request)

    @app.get("/fhir/{resource_type}")
    @app.get("/fhir/{resource_type}/{resource_id}")
    def read(resource_type: str, request: Request, resource_id: str | None = None):
        if resource_type in settings.hidden_resources:
            return _outcome(404, "not-supported", f"{resource_type} is not available on this server")
        claims = authorize(request, resource_type)
        if isinstance(claims, JSONResponse):
            return claims
        path = f"/{resource_type}" + (f"/{resource_id}" if resource_id else "")
        response = forward(path, request)
        log.info("%s GET %s -> %s", claims["sub"], path, response.status_code)
        return response

    @app.api_route("/fhir/{rest:path}", methods=["POST", "PUT", "PATCH", "DELETE"])
    def read_only(rest: str):
        return _outcome(405, "not-supported", "this gateway is read-only")

    return app


app = create_app()
