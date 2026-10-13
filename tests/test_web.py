from __future__ import annotations

import shutil

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from recall.store import AuditRow, DispositionRow, FlagRow, Run
from recall.web.app import create_app


@pytest.fixture
def web(settings, session_factory, gateway):
    return TestClient(create_app(settings, session_factory, http=gateway))


def start_run(web) -> int:
    r = web.post("/runs", follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].split("ran=")[1])


def test_empty_state_before_the_first_run(web):
    assert "No runs yet" in web.get("/worklist").text
    assert web.get("/").status_code == 200


def test_every_page_renders_after_a_run(web):
    start_run(web)
    for path in ["/", "/worklist", "/worklist?provider=pr-01", "/worklist?q=quintero", "/patients/p-0001",
                 "/patients/p-0003", "/patients/p-0004", "/patients/p-0009", "/measures", "/measures?all=1", "/rules",
                 "/audit", "/audit?action=fhir_read&patient=p-0001", "/report"]:
        r = web.get(path)
        assert r.status_code == 200, path


def test_overview_reports_the_export_fallback(web):
    run_id = start_run(web)
    page = web.get(f"/?ran={run_id}").text
    assert f"Run #{run_id} finished" in page
    assert "Staff export" in page


def test_worklist_shows_scripted_patients_with_their_reasons(web):
    start_run(web)
    page = web.get("/worklist?q=p-0007").text
    assert "Imogen Delacroix-Watts" in page and "Provider asked for a return visit" in page
    assert "Marisol" not in page


def test_patient_page_shows_the_four_measures(web):
    start_run(web)
    page = web.get("/patients/p-0001").text
    for label in ("Shockwave sessions", "Magnetic therapy sessions", "Last visit", "Injections, last 12 months"):
        assert label in page
    assert "MedicationAdministration/medadm-" in page  # the same-day second source is listed


def test_held_and_dropped_patients_explain_themselves(web):
    start_run(web)
    assert "Held back: Visit already booked" in web.get("/patients/p-0004").text
    assert "Dropped: LAPSED_ACTIVE_CARE" in web.get("/patients/p-0009").text


def test_logging_an_outcome_writes_the_outreach_log_and_audit(web, session_factory):
    start_run(web)
    r = web.post("/patients/p-0001/disposition", data={"outcome": "booked", "note": "Booked for Oct 20"},
                 follow_redirects=False)
    assert r.status_code == 303
    with session_factory() as s:
        d = s.scalars(select(DispositionRow)).one()
        assert (d.patient_id, d.outcome, d.rule_id) == ("p-0001", "booked", "POST_INJECTION_FOLLOWUP")
        assert s.scalars(select(AuditRow).where(AuditRow.action == "disposition_recorded")).one().patient_id == "p-0001"
    assert "Booked" in web.get("/report").text


def test_unknown_outcomes_are_rejected(web):
    start_run(web)
    assert web.post("/patients/p-0001/disposition", data={"outcome": "maybe"}).status_code == 400


def test_do_not_contact_outcome_keeps_patient_off_the_next_worklist(web, session_factory):
    start_run(web)
    web.post("/patients/p-0007/disposition", data={"outcome": "do_not_contact"})
    run_id = start_run(web)
    with session_factory() as s:
        flagged = set(s.scalars(select(FlagRow.patient_id).where(FlagRow.run_id == run_id)))
    assert "p-0007" not in flagged
    assert "Do-not-contact logged by staff" in web.get("/patients/p-0007").text


def test_viewing_a_source_record_is_audited(web, session_factory):
    start_run(web)
    page = web.get("/source/DocumentReference/doc-000001").text
    assert "Note text" in page and "Discussed injection options" in page
    page = web.get("/source/export/A-000001").text
    assert "Patient Account" in page and "DOB" not in page
    with session_factory() as s:
        rows = s.scalars(select(AuditRow).where(AuditRow.action == "source_viewed")).all()
        assert {r.detail.split("#")[0] for r in rows} == {"DocumentReference/doc-000001", "export:appointments.csv"}
        assert all(r.patient_id == "p-0001" for r in rows)


def test_csv_downloads_are_audited(web, session_factory):
    start_run(web)
    r = web.get("/worklist.csv")
    assert r.status_code == 200 and r.text.startswith("priority,patient_id")
    r = web.get("/measures.csv")
    assert "injections_last_12_months" in r.text.splitlines()[0]
    with session_factory() as s:
        actions = set(s.scalars(select(AuditRow.action)))
    assert {"worklist_exported", "measures_exported"} <= actions


def test_a_rule_sheet_edit_is_flagged_and_applied_on_the_next_run(settings, session_factory, gateway, tmp_path):
    from dataclasses import replace

    rules = tmp_path / "rules.yaml"
    shutil.copy(settings.rules_path, rules)
    client = TestClient(create_app(replace(settings, rules_path=rules), session_factory, http=gateway))
    first = start_run(client)
    assert "p-0001" in flagged_ids(session_factory, first)

    rules.write_text(rules.read_text(encoding="utf-8").replace("due_after_days: 28", "due_after_days: 60"),
                     encoding="utf-8")
    assert "has changed since run" in client.get("/").text

    second = start_run(client)
    with session_factory() as s:
        assert s.get(Run, first).rule_sha256 != s.get(Run, second).rule_sha256
    assert "p-0001" not in flagged_ids(session_factory, second)  # her injection was 44 days ago


def flagged_ids(session_factory, run_id: int) -> set[str]:
    with session_factory() as s:
        return set(s.scalars(select(FlagRow.patient_id).where(FlagRow.run_id == run_id)))


def test_a_broken_rule_sheet_is_reported_not_crashed(web, settings, tmp_path, session_factory):
    from dataclasses import replace

    rules = tmp_path / "rules.yaml"
    rules.write_text("rules:\n  - id: X\n    kind: nonsense\n", encoding="utf-8")
    client = TestClient(create_app(replace(settings, rules_path=rules), session_factory))
    assert "can't be loaded" in client.get("/").text
    assert "can't be loaded" in client.get("/rules").text


def test_records_seen_but_not_counted_are_shown_with_the_reason(web):
    start_run(web)
    osei = web.get("/patients/p-0010").text
    assert "Seen in the chart but not counted (1)" in osei
    assert "negated by" in osei and "declined" in osei and "DocumentReference/doc-" in osei
    pettigrew = web.get("/patients/p-0013").text
    assert "Seen in the chart but not counted (1)" in pettigrew
    assert "drug not on the injection list" in pettigrew and "MedicationAdministration/medadm-" in pettigrew
    assert "Influenza" not in pettigrew  # the drug name is only shown when the source is opened
    measures_page = web.get("/measures").text
    assert "Wilhelmina Osei" in measures_page and "1 not counted" in measures_page
