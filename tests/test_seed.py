"""The synthetic data must stay obviously synthetic."""

from __future__ import annotations

import json
import re

import httpx

from seed.__main__ import main as seed_main
from seed.generate import STORY, generate

from .conftest import AS_OF


def test_generation_is_deterministic():
    a, b = generate(AS_OF), generate(AS_OF)
    assert json.dumps(a.resources, sort_keys=True) == json.dumps(b.resources, sort_keys=True)
    assert a.export_rows == b.export_rows


def test_contact_details_are_fictional(practice):
    blob = json.dumps(practice.resources) + json.dumps(practice.export_rows)
    phones = re.findall(r"\(\d{3}\) \d{3}-\d{4}", blob)
    assert phones and all(re.fullmatch(r"\(\d{3}\) 555-01\d\d", p) for p in phones)
    emails = re.findall(r"[\w.+-]+@[\w.-]+", blob)
    assert emails and all(e.endswith("@example.com") for e in emails)


def test_every_reference_resolves(practice):
    known = {f"{rt}/{r['id']}" for rt, items in practice.resources.items() for r in items}
    refs = re.findall(r'"reference": "([A-Za-z]+/[\w-]+)"', json.dumps(practice.resources))
    assert refs and set(refs) <= known


def test_scripted_patients_exist(practice):
    names = {p["id"]: f"{p['name'][0]['given'][0]} {p['name'][0]['family']}" for p in practice.resources["Patient"]}
    for pid, story in STORY.items():
        assert names[pid] == story["name"]


def test_loader_posts_transaction_bundles(practice, tmp_path, monkeypatch):
    posted = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, json={"resourceType": "CapabilityStatement"})
        bundle = json.loads(request.content)
        posted.append(bundle)
        assert bundle["type"] == "transaction"
        assert all(e["request"]["method"] == "PUT" for e in bundle["entry"])
        return httpx.Response(200, json={"resourceType": "Bundle", "type": "transaction-response"})

    transport = httpx.MockTransport(handler)
    real_client, real_get = httpx.Client, httpx.get
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=transport, **kw))
    monkeypatch.setattr(httpx, "get", lambda url, **kw: real_client(transport=transport).get(url, **kw))
    seed_main(["load", "--fhir", "http://fhir.test/fhir", "--export-dir", str(tmp_path), "--as-of", AS_OF.isoformat()])
    assert real_get  # keep a reference so the monkeypatch is reverted cleanly
    total = sum(len(b["entry"]) for b in posted)
    assert total == sum(practice.counts().values())
    assert (tmp_path / "appointments.csv").read_text(encoding="utf-8").startswith("Appointment ID,Patient Account")
