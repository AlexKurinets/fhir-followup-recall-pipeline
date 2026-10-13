"""What the pipeline is allowed to keep, and proof that it keeps nothing else."""

from __future__ import annotations

import base64

from sqlalchemy import inspect, select, text

from recall.config import ROOT
from recall.extract import Extractor, patient_record
from recall.fieldmap import load_fieldmap
from recall.phrases import ListMatcher, build_catalog
from recall.pipeline import run_pipeline
from recall.rulesheet import load_rulesheet
from recall.store import PATIENT_LEVEL_TABLES, FactRow, make_engine

FIELDMAP = load_fieldmap(ROOT / "config" / "fieldmap.yaml")
SHEET = load_rulesheet(ROOT / "config" / "rules.yaml")


def test_patient_level_tables_only_have_mapped_columns(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'schema.db'}")
    insp = inspect(engine)
    for table in PATIENT_LEVEL_TABLES:
        columns = {c["name"] for c in insp.get_columns(table)}
        assert columns == set(FIELDMAP.storage[table]), table


def test_scopes_and_elements_come_from_the_field_map():
    assert FIELDMAP.scopes().split() == [f"system/{t}.read" for t in FIELDMAP.requests]
    assert "birthDate" not in FIELDMAP.elements("Patient")
    assert "address" not in FIELDMAP.elements("Patient")
    assert "ServiceRequest" not in FIELDMAP.requests


def test_extractor_ignores_fields_outside_the_map():
    resource = {
        "resourceType": "Patient", "id": "p-9", "name": [{"given": ["Ada"], "family": "Example"}],
        "birthDate": "1970-01-01", "address": [{"line": ["1 Example Street"]}],
        "telecom": [{"system": "email", "value": "ada@example.com"}, {"system": "phone", "value": "(212) 555-0100"}],
    }
    record = patient_record(resource)
    assert "1970" not in repr(record) and "Example Street" not in repr(record) and "@" not in repr(record)
    assert record.phone == "(212) 555-0100"


def test_unlisted_codes_produce_no_facts():
    ex = Extractor(SHEET, ListMatcher(build_catalog(SHEET)))
    oral_steroid = {"resourceType": "MedicationAdministration", "id": "m2", "status": "completed",
                    "subject": {"reference": "Patient/p-1"}, "effectiveDateTime": "2026-09-01T10:00:00-04:00",
                    "medicationCodeableConcept": {"text": "Methylprednisolone 4 mg tablet"},
                    "dosage": {"route": {"coding": [{"code": "26643006"}]}}}
    office_procedure = {"resourceType": "Procedure", "id": "x1", "status": "completed",
                        "subject": {"reference": "Patient/p-1"}, "performedDateTime": "2026-09-01",
                        "code": {"coding": [{"system": "http://www.ama-assn.org/go/cpt", "code": "97110"}]}}
    assert ex.medication_administration(oral_steroid) == []
    assert ex.procedure(office_procedure) == []


def test_a_near_miss_keeps_the_reason_but_not_the_drug_name():
    ex = Extractor(SHEET, ListMatcher(build_catalog(SHEET)))
    flu_shot = {"resourceType": "MedicationAdministration", "id": "m1", "status": "completed",
                "subject": {"reference": "Patient/p-1"}, "effectiveDateTime": "2026-09-01T10:00:00-04:00",
                "medicationCodeableConcept": {"text": "Influenza vaccine"},
                "dosage": {"route": {"coding": [{"code": "78421000"}]}}}
    [fact] = ex.medication_administration(flu_shot)
    assert (fact.kind, fact.code, fact.source_ref) == ("not_counted", "injection", "MedicationAdministration/m1")
    assert fact.detail == "drug not on the injection list"
    assert "influenza" not in repr(fact).lower() and "vaccine" not in repr(fact).lower()


def test_a_negated_note_keeps_the_signed_phrase_and_the_blocking_word_only():
    ex = Extractor(SHEET, ListMatcher(build_catalog(SHEET)))
    text = "Patient declined shockwave session today because of a sunburn on the heel."
    note = {"resourceType": "DocumentReference", "id": "d1", "subject": {"reference": "Patient/p-1"},
            "date": "2026-09-01T10:00:00-04:00",
            "content": [{"attachment": {"contentType": "text/plain",
                                        "data": base64.b64encode(text.encode()).decode()}}]}
    [fact] = ex.document_reference(note)
    assert (fact.kind, fact.code, fact.display) == ("not_counted", "shockwave", 'Note: "shockwave session"')
    assert fact.detail == 'negated by "declined"'
    assert "sunburn" not in repr(fact)


def test_note_text_is_never_stored(settings, session_factory, gateway, practice):
    run_id = run_pipeline(settings, session_factory, "tester@example.com", http=gateway)
    notes = [base64.b64decode(d["content"][0]["attachment"]["data"]).decode()
             for d in practice.resources["DocumentReference"]]
    distinctive = [n for n in notes if len(n) > 40][:50]
    with session_factory() as s:
        stored = " ".join(str(v) for row in s.execute(select(FactRow).where(FactRow.run_id == run_id)).scalars()
                          for v in (row.display, row.detail, row.code))
        dump = " ".join(str(r) for t in ("facts", "patients", "flags", "suppressions", "dropped", "audit_log")
                        for r in s.execute(text(f"SELECT * FROM {t}")).all())
    for note in distinctive:
        assert note not in stored and note not in dump


def test_birth_dates_and_addresses_never_reach_the_database(settings, session_factory, gateway, practice):
    run_pipeline(settings, session_factory, "tester@example.com", http=gateway)
    births = {p["birthDate"] for p in practice.resources["Patient"]}
    with session_factory() as s:
        dump = " ".join(str(r) for t in ("facts", "patients") for r in s.execute(text(f"SELECT * FROM {t}")).all())
    assert "Example Street" not in dump and "Sampleton" not in dump and "Sample Health" not in dump
    assert not any(b in dump for b in births)


def test_the_server_is_asked_for_mapped_elements_only(settings, session_factory, gateway, fake_fhir):
    run_pipeline(settings, session_factory, "tester@example.com", http=gateway)
    searches = [r for r in fake_fhir.requests if r.url.path.count("/") == 2 and "_elements" in r.url.params]
    assert searches
    for r in searches:
        resource_type = r.url.path.rsplit("/", 1)[1]
        assert r.url.params["_elements"] == FIELDMAP.elements(resource_type)
