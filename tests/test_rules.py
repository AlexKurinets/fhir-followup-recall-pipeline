from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest

from recall.config import ROOT
from recall.facts import APPOINTMENT, CODED, CONDITION, EXPORT, NOTE_MATCH, RETURN_REQUEST, VISIT, Fact, PatientRecord
from recall.rules import evaluate, measures
from recall.rulesheet import load_rulesheet

AS_OF = date(2026, 10, 13)
_seq = iter(range(1, 10_000))


@pytest.fixture(scope="module")
def sheet():
    return load_rulesheet(ROOT / "config" / "rules.yaml")


def ago(n: int) -> date:
    return AS_OF - timedelta(days=n)


def patient(**kw) -> PatientRecord:
    base = dict(patient_id="p-1", given_name="Test", family_name="Patient", phone="(212) 555-0100",
                provider_ref="pr-01", deceased=False, sms_consent=True, chart_do_not_contact=False,
                care_status="active")
    return PatientRecord(**{**base, **kw})


def fact(kind, days_ago, source=CODED, **kw) -> Fact:
    n = next(_seq)
    return Fact("p-1", kind, ago(days_ago) if days_ago is not None else None, source, kw.pop("ref", f"X/{n}"),
                id=n, **kw)


def visit(d):
    return fact(VISIT, d, display="Office visit", status="finished")


def injection(d, source=CODED):
    return fact("injection", d, source, code="20610")


def condition(code="M54.50"):
    return fact(CONDITION, 400, code=code, status="active")


def test_post_injection_followup_fires_after_due_window(sheet):
    out = evaluate(patient(), [visit(44), injection(44)], sheet, AS_OF)
    assert out.flagged.rule.id == "POST_INJECTION_FOLLOWUP"
    assert out.flagged.overdue_days(AS_OF) == 44 - 28


def test_post_injection_followup_waits_for_due_date(sheet):
    assert evaluate(patient(), [visit(20), injection(20)], sheet, AS_OF).flagged is None


def test_a_visit_after_the_injection_closes_it(sheet):
    assert evaluate(patient(), [visit(60), injection(60), visit(10)], sheet, AS_OF).flagged is None


def test_return_request_uses_interval_plus_grace(sheet):
    note = fact(RETURN_REQUEST, 34, NOTE_MATCH, code="RTC_4W")
    assert evaluate(patient(), [visit(34), note], sheet, AS_OF).flagged is None  # 28 + 7 not yet passed
    note = fact(RETURN_REQUEST, 36, NOTE_MATCH, code="RTC_4W")
    out = evaluate(patient(), [visit(36), note], sheet, AS_OF)
    assert out.flagged.rule.id == "PROVIDER_RETURN_REQUEST"
    assert out.flagged.evidence == [note]


def test_series_incomplete_counts_sessions_in_window(sheet):
    facts = [fact("shockwave", d) for d in (40, 33)] + [visit(33)]
    out = evaluate(patient(), facts, sheet, AS_OF)
    assert out.flagged.rule.id == "SHOCKWAVE_SERIES_INCOMPLETE"
    assert out.flagged.summary.startswith("2 of 4")


def test_completed_series_is_not_flagged(sheet):
    facts = [fact("shockwave", d) for d in (50, 43, 36, 29)] + [visit(29)]
    assert evaluate(patient(), facts, sheet, AS_OF).flagged is None


def test_missed_visit_not_rescheduled(sheet):
    noshow = fact(APPOINTMENT, 24, EXPORT, status="noshow")
    out = evaluate(patient(), [visit(50), noshow], sheet, AS_OF)
    assert out.flagged.rule.id == "MISSED_NOT_RESCHEDULED"


def test_missed_visit_that_was_rebooked_is_not_flagged(sheet):
    noshow = fact(APPOINTMENT, 24, EXPORT, status="noshow")
    rebooked = fact(APPOINTMENT, 17, EXPORT, status="fulfilled")
    assert evaluate(patient(), [visit(50), noshow, rebooked], sheet, AS_OF).flagged is None


def test_lapsed_requires_an_active_listed_diagnosis(sheet):
    assert evaluate(patient(), [visit(140)], sheet, AS_OF).flagged is None
    assert evaluate(patient(), [visit(140), condition("J45.909")], sheet, AS_OF).flagged is None
    assert evaluate(patient(), [visit(140), condition()], sheet, AS_OF).flagged.rule.id == "LAPSED_ACTIVE_CARE"


def test_rule_without_a_citable_fact_is_dropped(sheet):
    out = evaluate(patient(), [condition()], sheet, AS_OF)
    assert out.flagged is None
    assert [d.rule.id for d in out.dropped] == ["LAPSED_ACTIVE_CARE"]


def test_highest_priority_rule_leads_and_others_are_listed(sheet):
    out = evaluate(patient(), [visit(300), visit(160), injection(160), condition("G89.29")], sheet, AS_OF)
    assert out.flagged.rule.id == "POST_INJECTION_FOLLOWUP"
    assert [a.rule.id for a in out.also] == ["LAPSED_ACTIVE_CARE"]


@pytest.mark.parametrize("who, reason", [
    (dict(deceased=True), "deceased"),
    (dict(chart_do_not_contact=True), "chart_do_not_contact"),
    (dict(care_status="discharged"), "discharged"),
])
def test_suppressions(sheet, who, reason):
    out = evaluate(patient(**who), [visit(44), injection(44)], sheet, AS_OF)
    assert out.flagged is None
    assert out.suppressed_reason == reason
    assert [c.rule.id for c in out.suppressed_candidates] == ["POST_INJECTION_FOLLOWUP"]


def test_future_booking_suppresses(sheet):
    booked = fact(APPOINTMENT, -6, EXPORT, status="booked")
    out = evaluate(patient(), [visit(44), injection(44), booked], sheet, AS_OF)
    assert out.suppressed_reason == "future_appointment"


def test_staff_logged_do_not_contact_suppresses(sheet):
    out = evaluate(patient(), [visit(44), injection(44)], sheet, AS_OF, outreach_do_not_contact=True)
    assert out.suppressed_reason == "outreach_do_not_contact"


def test_channel_follows_consent(sheet):
    facts = [visit(44), injection(44)]
    assert evaluate(patient(sms_consent=True), facts, sheet, AS_OF).channel == "sms"
    assert evaluate(patient(sms_consent=False), facts, sheet, AS_OF).channel == "call"
    assert evaluate(patient(phone=None), facts, sheet, AS_OF).channel == "call"


def test_changing_the_rule_sheet_changes_the_outcome(sheet):
    facts = [visit(44), injection(44)]
    rules = tuple(replace(r, params={**r.params, "due_after_days": 60}) if r.id == "POST_INJECTION_FOLLOWUP" else r
                  for r in sheet.rules)
    assert evaluate(patient(), facts, replace(sheet, rules=rules), AS_OF).flagged is None


def test_measures_dedupe_same_day_documentation_and_window(sheet):
    facts = [
        injection(400),                       # outside 12 months
        injection(200),
        injection(44),
        fact("injection", 44, code="12130007", ref="MedicationAdministration/m1"),  # same day, other resource
        fact("injection", 44, NOTE_MATCH, code="SESSION_INJECTION", ref="DocumentReference/d1"),
        visit(44),
    ]
    m = measures(facts, sheet, AS_OF)
    assert len(m.injections_12mo) == 2
    assert m.last_injection.on == ago(44)
    assert len(m.last_injection.facts) == 3
    assert m.last_injection.primary.source_type == CODED
    assert m.last_visit.on == ago(44)


def test_note_only_sessions_are_counted_and_labelled(sheet):
    facts = [fact("shockwave", 50), fact("shockwave", 36, NOTE_MATCH, ref="DocumentReference/d9")]
    m = measures(facts, sheet, AS_OF)
    assert len(m.shockwave) == 2
    assert [s.from_note_only for s in m.shockwave] == [False, True]


def test_undated_facts_never_count(sheet):
    m = measures([fact("magnet", None)], sheet, AS_OF)
    assert m.magnet == []
