"""Shared fixtures.

Tests run the real gateway app (token endpoint, scope checks, resource hiding) in-process, with a
small in-memory FHIR server behind it that behaves like HAPI where the pipeline cares: paging via
_getpages links, `_elements` filtering, and a CapabilityStatement.
"""

from __future__ import annotations

import uuid
from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from gateway.app import GatewaySettings, create_app
from recall.config import ROOT, Settings
from recall.ehr.smart import ensure_keypair
from recall.store import make_session_factory
from seed.__main__ import write_export
from seed.generate import generate

AS_OF = date(2026, 10, 13)
BASE = "http://testserver"
TEST_SECRET = "test-secret-0123456789abcdef0123456789"


class FakeFhir:
    def __init__(self, resources: dict[str, list[dict]], public_base: str = f"{BASE}/fhir", page_cap: int = 100):
        self.store = {rt: {r["id"]: r for r in items} for rt, items in resources.items()}
        self.public_base = public_base
        self.page_cap = page_cap
        self.pages: dict[str, tuple[str, list[dict], list[str] | None]] = {}
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        parts = [p for p in request.url.path.split("/") if p][1:]  # drop the "fhir" prefix
        q = request.url.params
        if parts == ["metadata"]:
            return httpx.Response(200, json=self.capability())
        if not parts and "_getpages" in q:
            rt, items, elements = self.pages[q["_getpages"]]
            return httpx.Response(200, json=self._page(rt, items, elements, int(q["_getpagesoffset"]), int(q["_count"]),
                                                       q["_getpages"]))
        if len(parts) == 1:
            rt = parts[0]
            items = list(self.store.get(rt, {}).values())
            elements = q["_elements"].split(",") if "_elements" in q else None
            page_id = str(uuid.uuid4())
            self.pages[page_id] = (rt, items, elements)
            return httpx.Response(200, json=self._page(rt, items, elements, 0, min(int(q.get("_count", 20)),
                                                                                   self.page_cap), page_id))
        if len(parts) == 2:
            res = self.store.get(parts[0], {}).get(parts[1])
            if res is None:
                return httpx.Response(404, json={"resourceType": "OperationOutcome"})
            return httpx.Response(200, json=res)
        return httpx.Response(400, json={"resourceType": "OperationOutcome"})

    def capability(self) -> dict:
        return {"resourceType": "CapabilityStatement", "fhirVersion": "4.0.1",
                "rest": [{"mode": "server", "resource": [
                    {"type": rt, "interaction": [{"code": "read"}, {"code": "search-type"}]} for rt in self.store]}]}

    @staticmethod
    def _filter(resource: dict, elements: list[str] | None) -> dict:
        if not elements:
            return resource
        keep = {"resourceType", "id", "meta"}
        return {k: v for k, v in resource.items()
                if k in keep or any(k == e or (k.startswith(e) and k[len(e):][:1].isupper()) for e in elements)}

    def _page(self, rt: str, items: list[dict], elements, offset: int, count: int, page_id: str) -> dict:
        chunk = items[offset:offset + count]
        links = [{"relation": "self", "url": f"{self.public_base}/{rt}"}]
        if offset + count < len(items):
            links.append({"relation": "next", "url": f"{self.public_base}?_getpages={page_id}"
                                                     f"&_getpagesoffset={offset + count}&_count={count}"})
        return {"resourceType": "Bundle", "type": "searchset", "total": len(items), "link": links,
                "entry": [{"resource": self._filter(r, elements)} for r in chunk]}


@pytest.fixture(scope="session")
def practice():
    return generate(AS_OF)


@pytest.fixture
def keys_dir(tmp_path: Path) -> Path:
    d = tmp_path / "keys"
    ensure_keypair(d)
    return d


@pytest.fixture
def fake_fhir(practice) -> FakeFhir:
    return FakeFhir(practice.resources)


def make_gateway(fake: FakeFhir, keys_dir: Path, hidden=("Appointment",), clock=None, **overrides) -> TestClient:
    settings = GatewaySettings(upstream="http://upstream/fhir", public_base=BASE,
                               client_public_key_path=keys_dir / "client_public.pem",
                               hidden_resources=frozenset(hidden), signing_secret=TEST_SECRET, **overrides)
    kwargs = {"clock": clock} if clock else {}
    return TestClient(create_app(settings, upstream_transport=httpx.MockTransport(fake), **kwargs))


@pytest.fixture
def gateway(fake_fhir, keys_dir) -> TestClient:
    return make_gateway(fake_fhir, keys_dir)


@pytest.fixture
def settings(tmp_path: Path, keys_dir: Path, practice) -> Settings:
    export = write_export(practice.export_rows, tmp_path / "exports")
    return Settings(
        database_url=f"sqlite:///{tmp_path / 'recall.db'}",
        fhir_base_url=f"{BASE}/fhir",
        token_url=f"{BASE}/auth/token",
        client_id="recall-pipeline",
        client_key_dir=keys_dir,
        export_path=export,
        rules_path=ROOT / "config" / "rules.yaml",
        fieldmap_path=ROOT / "config" / "fieldmap.yaml",
        actor="tester@example.com",
        as_of_override=AS_OF.isoformat(),
        matcher="phrase-list",
        llm_model="claude-opus-5-5",
        retain_runs=5,
    )


@pytest.fixture
def session_factory(settings):
    return make_session_factory(settings.database_url)
