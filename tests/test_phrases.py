from __future__ import annotations

import pytest

from recall.config import ROOT
from recall.phrases import ListMatcher, build_catalog, normalize
from recall.rulesheet import load_rulesheet


@pytest.fixture(scope="module")
def matcher() -> ListMatcher:
    return ListMatcher(build_catalog(load_rulesheet(ROOT / "config" / "rules.yaml")))


def ids(matcher, text):
    return sorted(h.entry_id for h in matcher.match(text))


def test_normalize_expands_clinical_shorthand():
    assert normalize("RTC 4 wks") == ["return", "to", "clinic", "4", "weeks"]
    assert normalize("F/u in two weeks.") == ["follow", "up", "2", "weeks", "."]
    assert normalize("Follow-up in 3 mos") == ["follow", "up", "3", "months"]


@pytest.mark.parametrize("text, expected", [
    ("Plan: RTC 4 wks for reassessment.", ["RTC_4W"]),
    ("Return to clinic in four weeks.", ["RTC_4W"]),
    ("F/u 2 weeks.", ["RTC_2W"]),
    ("Follow up in 3 months.", ["RTC_3M"]),
    ("Recheck 6 wks", ["RTC_6W"]),
    ("Continue home exercise program; PRN.", []),
    ("RTC 5 weeks", []),  # not an interval on the signed list
])
def test_return_requests(matcher, text, expected):
    assert ids(matcher, text) == expected


@pytest.mark.parametrize("text, expected", [
    ("ESWT session 1 of 4, left heel, 2000 pulses.", ["SESSION_SHOCKWAVE"]),
    ("Radial shockwave session 3 completed, right Achilles insertion.", ["SESSION_SHOCKWAVE"]),
    ("EMTT session to lumbar paraspinals, 20 minutes.", ["SESSION_MAGNET"]),
    ("Right knee: triamcinolone 40 mg injection performed under ultrasound.", ["SESSION_INJECTION"]),
])
def test_performed_sessions_match(matcher, text, expected):
    assert ids(matcher, text) == expected


@pytest.mark.parametrize("text", [
    "Patient declined shockwave session today; will reconsider.",
    "No shockwave session today due to skin irritation.",
    "Plan: EMTT session next visit.",
    "Discussed ESWT, series of 4 recommended.",
    "Consider shockwave session if no improvement.",
    "Injection not performed, patient on anticoagulants.",
])
def test_negated_or_planned_sessions_do_not_match(matcher, text):
    assert ids(matcher, text) == []


def test_negation_does_not_cross_sentences(matcher):
    assert ids(matcher, "Declined PT. Shockwave session 2 completed.") == ["SESSION_SHOCKWAVE"]


def test_return_request_is_not_blocked_by_plan_wording(matcher):
    assert ids(matcher, "Plan: follow up 2 weeks.") == ["RTC_2W"]


def test_one_hit_per_entry(matcher):
    hits = matcher.match("Shockwave session 1. Radial shockwave to heel. ESWT session documented.")
    assert [h.entry_id for h in hits] == ["SESSION_SHOCKWAVE"]
