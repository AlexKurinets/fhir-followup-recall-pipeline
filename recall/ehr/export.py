"""Staff-run appointment export (CSV), used when Appointment isn't available over FHIR.

Scheduling reports usually carry more than the pipeline needs (date of birth, address, insurance).
Only the columns named in the field map are read; everything else in the row is ignored.
"""

from __future__ import annotations

import csv
from pathlib import Path

from ..extract import parse_day
from ..facts import APPOINTMENT, EXPORT, Fact
from ..fieldmap import FieldMap


class ExportError(RuntimeError):
    pass


def read_appointment_export(path: Path, fieldmap: FieldMap) -> tuple[list[Fact], int]:
    """Returns (facts, unmapped_status_rows)."""
    cols = fieldmap.export_columns
    status_map = fieldmap.export_status_map
    if not Path(path).exists():
        raise ExportError(f"appointment export not found at {path}")
    facts: list[Fact] = []
    unmapped = 0
    with open(path, newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        missing = [c for c in cols.values() if c not in (reader.fieldnames or [])]
        if missing:
            raise ExportError(f"appointment export is missing columns: {missing}")
        for row in reader:
            status = status_map.get(row[cols["status"]].strip())
            if status is None:
                unmapped += 1
                continue
            facts.append(Fact(
                patient_id=row[cols["patient_id"]].strip(),
                kind=APPOINTMENT,
                on=parse_day(row[cols["date"]].strip()),
                source_type=EXPORT,
                source_ref=f"export:{Path(path).name}#{row[cols['appointment_id']].strip()}",
                status=status,
                display="Appointment (export)",
                detail=row[cols["provider"]].strip(),
            ))
    return facts, unmapped


def read_export_row(path: Path, fieldmap: FieldMap, appointment_id: str) -> dict | None:
    """One row, limited to the mapped columns, for the source viewer."""
    cols = fieldmap.export_columns
    with open(path, newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            if row.get(cols["appointment_id"], "").strip() == appointment_id:
                return {label: row.get(label, "") for label in cols.values()}
    return None
