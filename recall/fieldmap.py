"""Load the minimum-necessary field map (config/fieldmap.yaml)."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


@dataclass(frozen=True)
class FieldMap:
    requests: dict[str, tuple[str, ...]]
    not_requested: dict[str, str]
    export_columns: dict[str, str]
    export_status_map: dict[str, str]
    storage: dict[str, tuple[str, ...]]

    @property
    def resource_types(self) -> tuple[str, ...]:
        return tuple(self.requests)

    def scopes(self) -> str:
        return " ".join(f"system/{t}.read" for t in self.requests)

    def elements(self, resource_type: str) -> str:
        return ",".join(self.requests[resource_type])


def load_fieldmap(path: Path) -> FieldMap:
    doc = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    export = doc.get("appointment_export") or {}
    return FieldMap(
        requests={k: tuple(v) for k, v in (doc.get("requests") or {}).items()},
        not_requested=dict(doc.get("not_requested") or {}),
        export_columns=dict(export.get("columns") or {}),
        export_status_map=dict(export.get("status_map") or {}),
        storage={k: tuple(v) for k, v in (doc.get("storage") or {}).items()},
    )
