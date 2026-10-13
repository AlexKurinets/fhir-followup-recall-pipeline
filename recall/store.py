"""Persistence. Each run is a snapshot: what was read, what fired, and why.

Patient-level tables (patients, facts, practitioners) hold only the columns listed in
config/fieldmap.yaml under `storage`; tests/test_minimum_necessary.py enforces that. Run detail
older than RETAIN_RUNS is pruned; the run summary, dispositions and the audit log are kept.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
    delete,
    event,
    select,
)
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from .facts import Fact


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    actor: Mapped[str] = mapped_column(String(200))
    as_of: Mapped[date] = mapped_column(Date)
    rule_version: Mapped[str] = mapped_column(String(100))
    rule_sha256: Mapped[str] = mapped_column(String(64))
    matcher: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="running")
    error: Mapped[str | None] = mapped_column(Text)
    coverage: Mapped[dict] = mapped_column(JSON, default=dict)
    token: Mapped[dict] = mapped_column(JSON, default=dict)
    stats: Mapped[dict] = mapped_column(JSON, default=dict)


class PatientRow(Base):
    __tablename__ = "patients"
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    patient_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    given_name: Mapped[str] = mapped_column(String(100))
    family_name: Mapped[str] = mapped_column(String(100))
    phone: Mapped[str | None] = mapped_column(String(40))
    provider_ref: Mapped[str | None] = mapped_column(String(64))
    deceased: Mapped[bool] = mapped_column(Boolean, default=False)
    sms_consent: Mapped[bool] = mapped_column(Boolean, default=False)
    chart_do_not_contact: Mapped[bool] = mapped_column(Boolean, default=False)
    care_status: Mapped[str] = mapped_column(String(20), default="active")


class PractitionerRow(Base):
    __tablename__ = "practitioners"
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), primary_key=True)
    practitioner_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(200))


class FactRow(Base):
    __tablename__ = "facts"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    on: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str | None] = mapped_column(String(40))
    code: Mapped[str | None] = mapped_column(String(40))
    code_system: Mapped[str | None] = mapped_column(String(200))
    display: Mapped[str | None] = mapped_column(String(200))
    source_type: Mapped[str] = mapped_column(String(20))
    source_ref: Mapped[str | None] = mapped_column(String(200))
    detail: Mapped[str | None] = mapped_column(String(200))


class FlagRow(Base):
    __tablename__ = "flags"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    provider_ref: Mapped[str | None] = mapped_column(String(64))
    rule_id: Mapped[str] = mapped_column(String(64))
    rule_label: Mapped[str] = mapped_column(String(200))
    priority: Mapped[int] = mapped_column(Integer)
    summary: Mapped[str] = mapped_column(String(300))
    due_on: Mapped[date | None] = mapped_column(Date)
    overdue_days: Mapped[int] = mapped_column(Integer, default=0)
    channel: Mapped[str] = mapped_column(String(10))
    evidence: Mapped[list] = mapped_column(JSON, default=list)   # FactRow ids
    also: Mapped[list] = mapped_column(JSON, default=list)       # [{rule_id, label, summary, evidence}]


class SuppressionRow(Base):
    __tablename__ = "suppressions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    reason: Mapped[str] = mapped_column(String(40))
    detail: Mapped[str] = mapped_column(String(300))
    rules: Mapped[list] = mapped_column(JSON, default=list)


class DroppedRow(Base):
    __tablename__ = "dropped"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), index=True)
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    rule_id: Mapped[str] = mapped_column(String(64))
    reason: Mapped[str] = mapped_column(String(300))


class DispositionRow(Base):
    """Outreach outcomes. These live in the outreach log only and are never written to the chart."""

    __tablename__ = "dispositions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    actor: Mapped[str] = mapped_column(String(200))
    patient_id: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[int | None] = mapped_column(Integer)
    flag_id: Mapped[int | None] = mapped_column(Integer)
    rule_id: Mapped[str | None] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(30))
    note: Mapped[str | None] = mapped_column(String(300))


class AuditRow(Base):
    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor: Mapped[str] = mapped_column(String(200))
    action: Mapped[str] = mapped_column(String(40), index=True)
    run_id: Mapped[int | None] = mapped_column(Integer)
    patient_id: Mapped[str | None] = mapped_column(String(64), index=True)
    resource_type: Mapped[str | None] = mapped_column(String(40))
    resource_count: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[str | None] = mapped_column(String(300))


def fact_from_row(r: FactRow) -> Fact:
    return Fact(patient_id=r.patient_id, kind=r.kind, on=r.on, source_type=r.source_type, source_ref=r.source_ref,
                code=r.code, code_system=r.code_system, display=r.display, status=r.status, detail=r.detail, id=r.id)


DISPOSITIONS = ("reached", "booked", "declined", "no_answer", "do_not_contact")
PATIENT_LEVEL_TABLES = {"patients": PatientRow, "facts": FactRow, "practitioners": PractitionerRow}


def make_engine(url: str) -> Engine:
    engine = create_engine(url, future=True)
    if url.startswith("sqlite"):
        @event.listens_for(engine, "connect")
        def _fk_on(dbapi_conn, _):  # noqa: ANN001
            dbapi_conn.execute("PRAGMA foreign_keys=ON")
    Base.metadata.create_all(engine)
    return engine


def make_session_factory(url: str) -> sessionmaker:
    return sessionmaker(bind=make_engine(url), expire_on_commit=False, future=True)


def prune_runs(session, keep: int) -> int:
    run_ids = session.scalars(select(Run.id).order_by(Run.id.desc())).all()
    old = run_ids[keep:]
    if not old:
        return 0
    for model in (FactRow, PatientRow, PractitionerRow, FlagRow, SuppressionRow, DroppedRow):
        session.execute(delete(model).where(model.run_id.in_(old)))
    return len(old)
