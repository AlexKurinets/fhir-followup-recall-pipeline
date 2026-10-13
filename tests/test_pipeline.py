from __future__ import annotations

from sqlalchemy import func, select

from recall.__main__ import check_story
from recall.pipeline import run_pipeline
from recall.store import AuditRow, FactRow, FlagRow, PatientRow, Run, SuppressionRow

from .conftest import make_gateway


def test_scripted_patients_come_out_as_expected(settings, session_factory, gateway):
    run_id = run_pipeline(settings, session_factory, "tester@example.com", http=gateway)
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.status == "ok", run.error
        assert check_story(s, run_id, settings) == []


def test_appointments_come_from_the_export_when_fhir_hides_them(settings, session_factory, gateway):
    run_id = run_pipeline(settings, session_factory, "tester@example.com", http=gateway)
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.coverage["Appointment"]["source"] == "export"
        assert "system/Appointment.read" not in run.token["scope"]
        sources = set(s.scalars(select(FactRow.source_type).where(FactRow.kind == "appointment")))
        assert sources == {"export"}


def test_appointments_switch_to_fhir_with_no_rule_changes(settings, session_factory, fake_fhir, keys_dir):
    gateway = make_gateway(fake_fhir, keys_dir, hidden=())
    run_id = run_pipeline(settings, session_factory, "tester@example.com", http=gateway)
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.coverage["Appointment"]["source"] == "fhir"
        assert "system/Appointment.read" in run.token["scope"]
        assert check_story(s, run_id, settings) == []


def test_every_flag_cites_dated_sourced_facts(settings, session_factory, gateway):
    run_id = run_pipeline(settings, session_factory, "tester@example.com", http=gateway)
    with session_factory() as s:
        flags = s.scalars(select(FlagRow).where(FlagRow.run_id == run_id)).all()
        assert len(flags) > 20
        for flag in flags:
            assert flag.evidence, flag.patient_id
            facts = s.scalars(select(FactRow).where(FactRow.id.in_(flag.evidence))).all()
            assert len(facts) == len(flag.evidence)
            assert all(f.on and f.source_ref and f.patient_id == flag.patient_id for f in facts)


def test_each_patient_appears_at_most_once(settings, session_factory, gateway):
    run_id = run_pipeline(settings, session_factory, "tester@example.com", http=gateway)
    with session_factory() as s:
        flagged = s.scalars(select(FlagRow.patient_id).where(FlagRow.run_id == run_id)).all()
        suppressed = s.scalars(select(SuppressionRow.patient_id).where(SuppressionRow.run_id == run_id)).all()
        assert len(flagged) == len(set(flagged))
        assert not set(flagged) & set(suppressed)


def test_every_patient_read_is_in_the_audit_log(settings, session_factory, gateway):
    run_id = run_pipeline(settings, session_factory, "tester@example.com", http=gateway)
    with session_factory() as s:
        patients = set(s.scalars(select(PatientRow.patient_id).where(PatientRow.run_id == run_id)))
        audited = set(s.scalars(select(AuditRow.patient_id).where(
            AuditRow.run_id == run_id, AuditRow.action == "fhir_read", AuditRow.resource_type == "Patient")))
        assert patients == audited
        actors = set(s.scalars(select(AuditRow.actor).where(AuditRow.run_id == run_id)))
        assert actors == {"tester@example.com"}


def test_a_rejected_token_fails_the_run_and_says_why(settings, session_factory, fake_fhir, keys_dir, tmp_path):
    from recall.ehr.smart import ensure_keypair

    other_keys = tmp_path / "other-keys"
    ensure_keypair(other_keys)
    gateway = make_gateway(fake_fhir, other_keys)  # gateway trusts a different key
    run_id = run_pipeline(settings, session_factory, "tester@example.com", http=gateway)
    with session_factory() as s:
        run = s.get(Run, run_id)
        assert run.status == "failed"
        assert "token endpoint returned 401" in run.error
        assert s.scalar(select(func.count()).select_from(FlagRow).where(FlagRow.run_id == run_id)) == 0


def test_old_runs_are_pruned_but_their_summaries_kept(settings, session_factory, gateway):
    from dataclasses import replace

    small = replace(settings, retain_runs=1)
    first = run_pipeline(small, session_factory, "tester@example.com", http=gateway)
    second = run_pipeline(small, session_factory, "tester@example.com", http=gateway)
    with session_factory() as s:
        assert s.get(Run, first).stats["flagged"] == s.get(Run, second).stats["flagged"]
        assert s.scalar(select(func.count()).select_from(FactRow).where(FactRow.run_id == first)) == 0
        assert s.scalar(select(func.count()).select_from(FactRow).where(FactRow.run_id == second)) > 0
