"""Load and validate the clinician-signed rule sheet (config/rules.yaml)."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

RULE_KINDS = {
    "post_event_followup": {"modality", "due_after_days", "stale_after_days"},
    "return_request": {"grace_days", "stale_after_days"},
    "series_incomplete": {"modality", "series_target", "series_window_days", "due_after_days"},
    "missed_visit": {"lookback_days", "due_after_days"},
    "lapsed": {"due_after_days", "stale_after_days", "condition_code_prefixes"},
}
SUPPRESSION_KEYS = ("deceased", "chart_do_not_contact", "outreach_do_not_contact", "discharged", "future_appointment")


class RuleSheetError(ValueError):
    pass


@dataclass(frozen=True)
class Coding:
    system: str
    code: str
    display: str = ""


@dataclass(frozen=True)
class Modality:
    key: str
    label: str
    procedure_codes: tuple[Coding, ...] = ()
    medications: tuple[str, ...] = ()
    routes: tuple[Coding, ...] = ()
    note_phrases: tuple[str, ...] = ()

    def matches_procedure(self, system: str | None, code: str | None) -> Coding | None:
        for c in self.procedure_codes:
            if c.code == code and (system is None or c.system == system):
                return c
        return None


@dataclass(frozen=True)
class ReturnRequest:
    id: str
    interval_days: int
    phrases: tuple[str, ...]


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    label: str
    priority: int
    params: dict[str, Any] = field(default_factory=dict)

    def __getitem__(self, key: str) -> Any:
        return self.params[key]


@dataclass(frozen=True)
class RuleSheet:
    version: str
    status: str
    signed_by: str
    lookback_days: int
    visit_statuses: tuple[str, ...]
    modalities: dict[str, Modality]
    return_requests: tuple[ReturnRequest, ...]
    rules: tuple[Rule, ...]
    suppress_when: dict[str, bool]
    sms_requires_consent: bool
    sha256: str
    raw_text: str

    @property
    def short_hash(self) -> str:
        return self.sha256[:10]

    def rule(self, rule_id: str) -> Rule:
        for r in self.rules:
            if r.id == rule_id:
                return r
        raise KeyError(rule_id)


def _codings(items: list[dict] | None, where: str) -> tuple[Coding, ...]:
    out = []
    for item in items or []:
        if "code" not in item:
            raise RuleSheetError(f"{where}: every code needs a 'code'")
        out.append(Coding(system=item.get("system", ""), code=str(item["code"]), display=item.get("display", "")))
    return tuple(out)


def parse_rulesheet(text: str) -> RuleSheet:
    try:
        doc = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise RuleSheetError(f"rules.yaml is not valid YAML: {exc}") from exc

    modalities: dict[str, Modality] = {}
    for key, m in (doc.get("modalities") or {}).items():
        mar = m.get("medication_administration") or {}
        modalities[key] = Modality(
            key=key,
            label=m.get("label", key),
            procedure_codes=_codings(m.get("procedure_codes"), f"modalities.{key}.procedure_codes"),
            medications=tuple(s.lower() for s in mar.get("medications", [])),
            routes=_codings(mar.get("routes"), f"modalities.{key}.medication_administration.routes"),
            note_phrases=tuple(m.get("note_phrases") or ()),
        )

    returns = tuple(
        ReturnRequest(id=r["id"], interval_days=int(r["interval_days"]), phrases=tuple(r["phrases"]))
        for r in doc.get("return_requests") or []
    )

    rules = []
    seen: set[str] = set()
    for raw in doc.get("rules") or []:
        rid, kind = raw.get("id"), raw.get("kind")
        if not rid or kind not in RULE_KINDS:
            raise RuleSheetError(f"rule {rid!r}: unknown kind {kind!r}; expected one of {sorted(RULE_KINDS)}")
        if rid in seen:
            raise RuleSheetError(f"rule {rid!r} is defined twice")
        seen.add(rid)
        params = {k: v for k, v in raw.items() if k not in {"id", "kind", "label", "priority"}}
        missing = RULE_KINDS[kind] - params.keys()
        if missing:
            raise RuleSheetError(f"rule {rid}: missing {sorted(missing)}")
        if "modality" in params and params["modality"] not in modalities:
            raise RuleSheetError(f"rule {rid}: modality {params['modality']!r} is not defined")
        rules.append(Rule(id=rid, kind=kind, label=raw.get("label", rid), priority=int(raw.get("priority", 3)),
                          params=params))

    suppress = {k: bool((doc.get("suppress_when") or {}).get(k, False)) for k in SUPPRESSION_KEYS}

    return RuleSheet(
        version=str(doc.get("version", "unversioned")),
        status=str(doc.get("status", "DRAFT")),
        signed_by=str(doc.get("signed_by") or ""),
        lookback_days=int(doc.get("lookback_days", 365)),
        visit_statuses=tuple((doc.get("visit") or {}).get("encounter_statuses", ["finished"])),
        modalities=modalities,
        return_requests=returns,
        rules=tuple(rules),
        suppress_when=suppress,
        sms_requires_consent=bool((doc.get("outreach") or {}).get("sms_requires_consent", True)),
        sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        raw_text=text,
    )


def load_rulesheet(path: Path) -> RuleSheet:
    return parse_rulesheet(Path(path).read_text(encoding="utf-8"))


def describe(rule: Rule, sheet: RuleSheet) -> str:
    """Plain-language statement of when a rule fires, for the Rules page and the reason log."""
    p = rule.params
    if rule.kind == "post_event_followup":
        label = sheet.modalities[p["modality"]].label.lower()
        return (f"The last {label} was at least {p['due_after_days']} days ago (and no more than "
                f"{p['stale_after_days']}), and there has been no visit since.")
    if rule.kind == "return_request":
        return (f"A note asked for a return visit (a phrase from the return list), that interval plus "
                f"{p['grace_days']} days has passed, and there has been no visit since the note.")
    if rule.kind == "series_incomplete":
        label = sheet.modalities[p["modality"]].label.lower()
        return (f"Fewer than {p['series_target']} {label}s in the last {p['series_window_days']} days, and the "
                f"last one was at least {p['due_after_days']} days ago.")
    if rule.kind == "missed_visit":
        return (f"A no-show in the last {p['lookback_days']} days, at least {p['due_after_days']} days ago, with "
                f"no visit or booking after it.")
    if rule.kind == "lapsed":
        prefixes = ", ".join(p["condition_code_prefixes"])
        return (f"An active diagnosis ({prefixes}) and the last visit was {p['due_after_days']} to "
                f"{p['stale_after_days']} days ago.")
    return rule.kind
