"""The only shapes patient data takes after it leaves the extractor.

A Fact is one dated, sourced statement from the chart ("injection 20610 on 2026-08-30, from
Procedure/inj-0001-3"). Rules and counts are computed from facts only, and every flag carries the
facts that fired it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

# Fact kinds
VISIT = "visit"
CONDITION = "condition"
APPOINTMENT = "appointment"
RETURN_REQUEST = "return_request"

# Source types
CODED = "coded"            # a coded FHIR resource (Procedure, MedicationAdministration, Encounter, ...)
NOTE_MATCH = "note_match"  # a phrase from the signed list matched in a note
EXPORT = "export"          # a row in the staff-run appointment export

# Appointment statuses that mean the patient is (or was) on the book
BOOKED_STATUSES = {"booked", "pending", "proposed", "arrived", "checked-in", "fulfilled"}
FUTURE_STATUSES = {"booked", "pending", "proposed"}


@dataclass(frozen=True)
class PatientRecord:
    patient_id: str
    given_name: str
    family_name: str
    phone: str | None
    provider_ref: str | None
    deceased: bool
    sms_consent: bool
    chart_do_not_contact: bool
    care_status: str

    @property
    def display_name(self) -> str:
        return f"{self.given_name} {self.family_name}".strip()


@dataclass(frozen=True)
class Fact:
    patient_id: str
    kind: str
    on: date | None
    source_type: str
    source_ref: str | None
    code: str | None = None
    code_system: str | None = None
    display: str | None = None
    status: str | None = None
    detail: str | None = None
    id: int | None = None

    @property
    def is_citable(self) -> bool:
        """A fact can back a flag only if it has a date and points at a source."""
        return self.on is not None and bool(self.source_ref)

    def summary(self) -> str:
        bits = [self.display or self.code or self.kind]
        if self.on:
            bits.append(f"on {self.on.isoformat()}")
        return " ".join(bits)
