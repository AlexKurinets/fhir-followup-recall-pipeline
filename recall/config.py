"""Runtime settings, read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _env(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(default_factory=lambda: _env("DATABASE_URL", f"sqlite:///{ROOT / 'recall.db'}"))
    fhir_base_url: str = field(default_factory=lambda: _env("FHIR_BASE_URL", "http://localhost:8081/fhir"))
    token_url: str = field(default_factory=lambda: _env("TOKEN_URL", "http://localhost:8081/auth/token"))
    client_id: str = field(default_factory=lambda: _env("CLIENT_ID", "recall-pipeline"))
    client_key_dir: Path = field(default_factory=lambda: Path(_env("CLIENT_KEY_DIR", str(ROOT / ".keys"))))
    export_path: Path = field(
        default_factory=lambda: Path(_env("EXPORT_PATH", str(ROOT / "data" / "exports" / "appointments.csv")))
    )
    rules_path: Path = field(default_factory=lambda: Path(_env("RULES_PATH", str(ROOT / "config" / "rules.yaml"))))
    fieldmap_path: Path = field(
        default_factory=lambda: Path(_env("FIELDMAP_PATH", str(ROOT / "config" / "fieldmap.yaml")))
    )
    actor: str = field(default_factory=lambda: _env("DEMO_ACTOR", "dana.reyes@example.com"))
    as_of_override: str = field(default_factory=lambda: _env("AS_OF", ""))
    matcher: str = field(default_factory=lambda: _env("MATCHER", "phrase-list"))
    llm_model: str = field(default_factory=lambda: _env("LLM_MODEL", "claude-opus-5-5"))
    retain_runs: int = field(default_factory=lambda: int(_env("RETAIN_RUNS", "5")))

    def as_of(self) -> date:
        return date.fromisoformat(self.as_of_override) if self.as_of_override else date.today()
