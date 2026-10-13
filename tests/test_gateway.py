from __future__ import annotations

import time
import uuid

import httpx
import jwt
import pytest

from recall.ehr.smart import ASSERTION_TYPE, SmartBackendClient, TokenError, ensure_keypair

from .conftest import BASE

TOKEN_URL = f"{BASE}/auth/token"


def client_for(gateway, keys_dir, scopes="system/Patient.read", clock=time.time) -> SmartBackendClient:
    key = (keys_dir / "client_private.pem").read_bytes()
    return SmartBackendClient(TOKEN_URL, "recall-pipeline", key, scopes, gateway, clock=clock)


def assertion(keys_dir, **overrides) -> str:
    now = int(time.time())
    claims = {"iss": "recall-pipeline", "sub": "recall-pipeline", "aud": TOKEN_URL, "exp": now + 300,
              "iat": now, "jti": str(uuid.uuid4()), **overrides}
    return jwt.encode(claims, (keys_dir / "client_private.pem").read_bytes(), algorithm="RS384")


def request_token(gateway, a: str, scope="system/Patient.read") -> httpx.Response:
    return gateway.post("/auth/token", data={"grant_type": "client_credentials", "scope": scope,
                                             "client_assertion_type": ASSERTION_TYPE, "client_assertion": a})


def test_valid_assertion_gets_a_short_lived_token(gateway, keys_dir):
    r = request_token(gateway, assertion(keys_dir))
    assert r.status_code == 200
    body = r.json()
    assert body["expires_in"] == 300
    assert body["scope"] == "system/Patient.read"


@pytest.mark.parametrize("override, message", [
    ({"aud": "http://elsewhere/token"}, "Audience"),
    ({"exp": int(time.time()) - 60}, "expired"),
    ({"iss": "someone-else", "sub": "someone-else"}, "registered client id"),
    ({"exp": int(time.time()) + 3600}, "exceeds 5 minutes"),
])
def test_bad_assertions_are_rejected(gateway, keys_dir, override, message):
    r = request_token(gateway, assertion(keys_dir, **override))
    assert r.status_code == 401
    assert message.lower() in r.json()["error_description"].lower()


def test_assertion_signed_with_another_key_is_rejected(gateway, tmp_path):
    other = tmp_path / "other"
    ensure_keypair(other)
    r = request_token(gateway, assertion(other))
    assert r.status_code == 401


def test_an_assertion_cannot_be_replayed(gateway, keys_dir):
    a = assertion(keys_dir)
    assert request_token(gateway, a).status_code == 200
    r = request_token(gateway, a)
    assert r.status_code == 401
    assert "already used" in r.json()["error_description"]


def test_unknown_scopes_are_not_granted(gateway, keys_dir):
    r = request_token(gateway, assertion(keys_dir), scope="system/Patient.read system/Patient.write user/*.*")
    assert r.json()["scope"] == "system/Patient.read"
    assert request_token(gateway, assertion(keys_dir), scope="system/Patient.write").status_code == 400


def test_requests_need_a_token(gateway):
    r = gateway.get("/fhir/Patient")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == "Bearer"


def test_token_only_reads_what_its_scopes_allow(gateway, keys_dir):
    token = client_for(gateway, keys_dir, scopes="system/Patient.read").token()
    headers = {"Authorization": f"Bearer {token}"}
    assert gateway.get("/fhir/Patient", headers=headers).status_code == 200
    r = gateway.get("/fhir/Procedure", headers=headers)
    assert r.status_code == 403
    assert r.json()["resourceType"] == "OperationOutcome"


def test_gateway_is_read_only(gateway, keys_dir):
    token = client_for(gateway, keys_dir).token()
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        r = gateway.request(method, "/fhir/Patient/p-0001", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 405


def test_hidden_resources_are_absent_from_metadata_and_unreachable(gateway, keys_dir):
    cap = gateway.get("/fhir/metadata").json()
    types = {r["type"] for r in cap["rest"][0]["resource"]}
    assert "Appointment" not in types and "Patient" in types
    token = client_for(gateway, keys_dir, scopes="system/Appointment.read").token()
    assert gateway.get("/fhir/Appointment", headers={"Authorization": f"Bearer {token}"}).status_code == 404


def test_metadata_advertises_the_token_endpoint(gateway):
    cap = gateway.get("/fhir/metadata").json()
    ext = cap["rest"][0]["security"]["extension"][0]["extension"][0]
    assert ext == {"url": "token", "valueUri": TOKEN_URL}
    smart = gateway.get("/.well-known/smart-configuration").json()
    assert smart["token_endpoint"] == TOKEN_URL
    assert smart["token_endpoint_auth_methods_supported"] == ["private_key_jwt"]


def test_expired_access_tokens_are_rejected(gateway):
    secret = gateway.app.state.settings.signing_secret
    past = int(time.time()) - 600
    token = jwt.encode({"sub": "recall-pipeline", "scope": "system/Patient.read", "iat": past, "exp": past + 300},
                       secret, algorithm="HS256")
    r = gateway.get("/fhir/Patient", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 401
    assert r.json()["issue"][0]["code"] == "expired"


def test_forged_access_tokens_are_rejected(gateway):
    token = jwt.encode({"sub": "x", "scope": "system/*.read", "exp": int(time.time()) + 300},
                       "not-the-gateway-secret-0123456789abcdef", algorithm="HS256")
    assert gateway.get("/fhir/Patient", headers={"Authorization": f"Bearer {token}"}).status_code == 401


def test_client_reuses_its_token_until_near_expiry(gateway, keys_dir):
    now = [time.time() - 280]  # first token issued 280 s ago, so it is inside the 30 s refresh margin now
    client = client_for(gateway, keys_dir, clock=lambda: now[0])
    first = client.token()
    assert client.token() == first
    now[0] = time.time()
    assert client.token() != first
    assert client.tokens_issued == 2


def test_client_surfaces_token_errors(gateway, tmp_path):
    other = tmp_path / "other"
    ensure_keypair(other)
    client = SmartBackendClient(TOKEN_URL, "recall-pipeline", (other / "client_private.pem").read_bytes(),
                                "system/Patient.read", gateway)
    with pytest.raises(TokenError, match="401"):
        client.token()


def test_paging_links_are_followed_through_the_gateway(gateway, keys_dir, fake_fhir):
    from recall.ehr.fhir import FhirSource

    auth = client_for(gateway, keys_dir, scopes="system/Encounter.read")
    source = FhirSource(f"{BASE}/fhir", auth, gateway, page_size=100)
    encounters = list(source.search("Encounter", "subject,status,period,type"))
    assert len(encounters) == len(fake_fhir.store["Encounter"])
    assert source.requests_made > 1
    assert all(set(e) <= {"resourceType", "id", "meta", "subject", "status", "period", "type"} for e in encounters)
