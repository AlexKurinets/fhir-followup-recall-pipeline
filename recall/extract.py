"""Turn FHIR resources into PatientRecords and Facts.

This is where minimum necessary is enforced in code: each function reads the handful of elements the
rule sheet needs and returns nothing else. Resources that don't match a signed code, route or phrase
produce no fact at all. The one exception is a near miss, which is kept as a NOT_COUNTED fact so staff can
see why a count is lower than the chart suggests: a session phrase that is negated or only planned, or a
drug outside the signed list given by a signed route. A near miss keeps the reason and the source
reference only, never the note text or the drug name; opening the source fetches it live.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass, field
from datetime import date

from .facts import (
    APPOINTMENT,
    CODED,
    CONDITION,
    NOT_COUNTED,
    NOTE_MATCH,
    RETURN_REQUEST,
    VISIT,
    Fact,
    PatientRecord,
)
from .phrases import Matcher, MatcherError, why_set_aside
from .rulesheet import RuleSheet

EXT_BASE = "https://example.com/fhir/StructureDefinition/"
EXT_SMS_CONSENT = EXT_BASE + "sms-consent"
EXT_DO_NOT_CONTACT = EXT_BASE + "do-not-contact"
EXT_CARE_STATUS = EXT_BASE + "care-status"


@dataclass
class ExtractStats:
    rejected_undated: int = 0
    notes_scanned: int = 0
    matcher_errors: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)

    def count(self, fact: Fact) -> None:
        self.by_kind[fact.kind] = self.by_kind.get(fact.kind, 0) + 1


def parse_day(value: str | None) -> date | None:
    """FHIR date/dateTime/instant to a calendar day, keeping the local date as recorded."""
    if not value or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def ref_id(reference: str | None, resource_type: str) -> str | None:
    prefix = f"{resource_type}/"
    if reference and reference.startswith(prefix):
        return reference[len(prefix):]
    return None


def subject_id(resource: dict) -> str | None:
    return ref_id((resource.get("subject") or {}).get("reference"), "Patient")


def _ext(resource: dict, url: str):
    for e in resource.get("extension") or []:
        if e.get("url") == url:
            for key, value in e.items():
                if key.startswith("value"):
                    return value
    return None


def patient_record(resource: dict) -> PatientRecord:
    name = (resource.get("name") or [{}])[0]
    phones = [t for t in resource.get("telecom") or [] if t.get("system") in ("phone", "sms")]
    phones.sort(key=lambda t: 0 if t.get("use") == "mobile" else 1)
    gp = (resource.get("generalPractitioner") or [{}])[0].get("reference")
    return PatientRecord(
        patient_id=resource["id"],
        given_name=(name.get("given") or [""])[0],
        family_name=name.get("family", ""),
        phone=phones[0]["value"] if phones else None,
        provider_ref=ref_id(gp, "Practitioner"),
        deceased=bool(resource.get("deceasedBoolean")) or bool(resource.get("deceasedDateTime")),
        sms_consent=_ext(resource, EXT_SMS_CONSENT) is True,
        chart_do_not_contact=_ext(resource, EXT_DO_NOT_CONTACT) is True,
        care_status=_ext(resource, EXT_CARE_STATUS) or "active",
    )


def practitioner_name(resource: dict) -> str:
    name = (resource.get("name") or [{}])[0]
    if name.get("text"):
        return name["text"]
    parts = [*name.get("prefix", []), *name.get("given", []), name.get("family", "")]
    suffix = ", ".join(name.get("suffix", []))
    return " ".join(p for p in parts if p) + (f", {suffix}" if suffix else "")


class Extractor:
    def __init__(self, sheet: RuleSheet, matcher: Matcher):
        self.sheet = sheet
        self.matcher = matcher
        self.stats = ExtractStats()

    def _keep(self, fact: Fact, out: list[Fact]) -> None:
        if fact.on is None:
            self.stats.rejected_undated += 1
            return
        self.stats.count(fact)
        out.append(fact)

    def encounter(self, r: dict) -> list[Fact]:
        out: list[Fact] = []
        pid = subject_id(r)
        if pid and r.get("status") in self.sheet.visit_statuses:
            type_text = ((r.get("type") or [{}])[0]).get("text")
            self._keep(Fact(pid, VISIT, parse_day((r.get("period") or {}).get("start")), CODED,
                            f"Encounter/{r['id']}", display=type_text or "Visit", status=r.get("status")), out)
        return out

    def procedure(self, r: dict) -> list[Fact]:
        out: list[Fact] = []
        pid = subject_id(r)
        if not pid or r.get("status") != "completed":
            return out
        day = parse_day(r.get("performedDateTime") or (r.get("performedPeriod") or {}).get("start"))
        for coding in (r.get("code") or {}).get("coding") or []:
            for modality in self.sheet.modalities.values():
                hit = modality.matches_procedure(coding.get("system"), coding.get("code"))
                if hit:
                    self._keep(Fact(pid, modality.key, day, CODED, f"Procedure/{r['id']}", code=hit.code,
                                    code_system=hit.system, display=hit.display or modality.label), out)
                    return out
        return out

    def medication_administration(self, r: dict) -> list[Fact]:
        out: list[Fact] = []
        pid = subject_id(r)
        if not pid or r.get("status") != "completed":
            return out
        med = r.get("medicationCodeableConcept") or {}
        med_text = (med.get("text") or " ".join(c.get("display", "") for c in med.get("coding") or [])).lower()
        route_codes = {c.get("code") for c in ((r.get("dosage") or {}).get("route") or {}).get("coding") or []}
        day = parse_day(r.get("effectiveDateTime") or (r.get("effectivePeriod") or {}).get("start"))
        for modality in self.sheet.modalities.values():
            if not modality.medications:
                continue
            drug = next((m for m in modality.medications if m in med_text), None)
            route = next((c for c in modality.routes if c.code in route_codes), None)
            if drug and route:
                self._keep(Fact(pid, modality.key, day, CODED, f"MedicationAdministration/{r['id']}",
                                code=route.code, code_system=route.system,
                                display=f"{drug.capitalize()}, {route.display.lower()}"), out)
                return out
        for modality in self.sheet.modalities.values():
            route = next((c for c in modality.routes if c.code in route_codes), None)
            if modality.medications and route:
                self._keep(Fact(pid, NOT_COUNTED, day, CODED, f"MedicationAdministration/{r['id']}",
                                code=modality.key, display=f"Medication given by {route.display.lower()}",
                                detail=f"drug not on the {modality.label.lower()} list"), out)
                break
        return out

    def condition(self, r: dict) -> list[Fact]:
        out: list[Fact] = []
        pid = subject_id(r)
        status = {c.get("code") for c in (r.get("clinicalStatus") or {}).get("coding") or []}
        if not pid or "active" not in status:
            return out
        prefixes = {p for rule in self.sheet.rules if rule.kind == "lapsed" for p in rule["condition_code_prefixes"]}
        for coding in (r.get("code") or {}).get("coding") or []:
            code = coding.get("code") or ""
            if any(code.startswith(p) for p in prefixes):
                self._keep(Fact(pid, CONDITION, parse_day(r.get("onsetDateTime") or r.get("recordedDate")), CODED,
                                f"Condition/{r['id']}", code=code, code_system=coding.get("system"),
                                display=coding.get("display") or code, status="active"), out)
                break
        return out

    def document_reference(self, r: dict) -> list[Fact]:
        out: list[Fact] = []
        pid = subject_id(r)
        if not pid:
            return out
        text = _note_text(r)
        if not text:
            return out
        self.stats.notes_scanned += 1
        try:
            hits, set_aside = self.matcher.scan(text)
        except MatcherError:
            self.stats.matcher_errors += 1
            return out
        finally:
            del text  # the note is not kept past this call
        day = parse_day(r.get("date"))
        for hit in hits:
            kind = RETURN_REQUEST if hit.kind == RETURN_REQUEST else hit.kind
            self._keep(Fact(pid, kind, day, NOTE_MATCH, f"DocumentReference/{r['id']}", code=hit.entry_id,
                            display=f'Note: "{hit.phrase}"', detail=f"matcher={hit.matcher}"), out)
        for hit in set_aside:
            self._keep(Fact(pid, NOT_COUNTED, day, NOTE_MATCH, f"DocumentReference/{r['id']}", code=hit.kind,
                            display=f'Note: "{hit.phrase}"', detail=why_set_aside(hit.set_aside_by)), out)
        return out

    def appointment(self, r: dict) -> list[Fact]:
        out: list[Fact] = []
        pid = None
        provider = None
        for p in r.get("participant") or []:
            ref = (p.get("actor") or {}).get("reference")
            pid = pid or ref_id(ref, "Patient")
            provider = provider or ref_id(ref, "Practitioner")
        if pid:
            self._keep(Fact(pid, APPOINTMENT, parse_day(r.get("start")), CODED, f"Appointment/{r['id']}",
                            status=r.get("status"), display="Appointment", detail=provider), out)
        return out

    def dispatch(self, resource_type: str, resource: dict) -> list[Fact]:
        handler = {
            "Encounter": self.encounter,
            "Procedure": self.procedure,
            "MedicationAdministration": self.medication_administration,
            "Condition": self.condition,
            "DocumentReference": self.document_reference,
            "Appointment": self.appointment,
        }.get(resource_type)
        return handler(resource) if handler else []


def _note_text(r: dict) -> str:
    parts = []
    for content in r.get("content") or []:
        att = content.get("attachment") or {}
        if not str(att.get("contentType", "text/plain")).startswith("text/"):
            continue
        data = att.get("data")
        if not data:
            continue
        try:
            parts.append(base64.b64decode(data).decode("utf-8", errors="replace"))
        except (binascii.Error, ValueError):
            continue
    return "\n".join(parts)
