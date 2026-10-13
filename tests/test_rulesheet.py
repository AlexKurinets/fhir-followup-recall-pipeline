from __future__ import annotations

import pytest

from recall.config import ROOT
from recall.rulesheet import RuleSheetError, describe, load_rulesheet, parse_rulesheet


def test_shipped_rule_sheet_loads_and_every_rule_has_a_description():
    sheet = load_rulesheet(ROOT / "config" / "rules.yaml")
    assert sheet.status == "DRAFT"
    assert len(sheet.sha256) == 64
    assert [r.priority for r in sheet.rules] == sorted(r.priority for r in sheet.rules)
    for rule in sheet.rules:
        assert describe(rule, sheet) and describe(rule, sheet) != rule.kind


@pytest.mark.parametrize("yaml_text, message", [
    ("rules:\n  - id: A\n    kind: magic\n", "unknown kind"),
    ("rules:\n  - id: A\n    kind: missed_visit\n", "missing"),
    ("rules:\n  - {id: A, kind: missed_visit, lookback_days: 1, due_after_days: 1}\n"
     "  - {id: A, kind: missed_visit, lookback_days: 1, due_after_days: 1}\n", "defined twice"),
    ("rules:\n  - {id: A, kind: post_event_followup, modality: laser, due_after_days: 1, stale_after_days: 2}\n",
     "not defined"),
    ("rules: [", "not valid YAML"),
])
def test_invalid_sheets_fail_loudly(yaml_text, message):
    with pytest.raises(RuleSheetError, match=message):
        parse_rulesheet(yaml_text)


def test_hash_changes_with_any_edit():
    text = (ROOT / "config" / "rules.yaml").read_text(encoding="utf-8")
    edited = text.replace("due_after_days: 28", "due_after_days: 30")
    assert parse_rulesheet(text).sha256 != parse_rulesheet(edited).sha256
