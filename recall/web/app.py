"""Staff dashboard: runs, worklist, patient detail, measures, rules, audit log, pilot report."""

from __future__ import annotations

import base64
import csv
import io
import json
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

import httpx
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings
from ..ehr.export import read_export_row
from ..ehr.fhir import FhirError, FhirSource
from ..ehr.smart import SmartBackendClient, TokenError, ensure_keypair
from ..extract import subject_id
from ..facts import APPOINTMENT, CODED, EXPORT, NOTE_MATCH
from ..fieldmap import load_fieldmap
from ..pipeline import run_pipeline
from ..rules import measures
from ..rulesheet import RuleSheetError, describe, load_rulesheet
from ..store import (
    DISPOSITIONS,
    AuditRow,
    DispositionRow,
    DroppedRow,
    FactRow,
    FlagRow,
    PatientRow,
    PractitionerRow,
    Run,
    SuppressionRow,
    fact_from_row,
    make_session_factory,
)

HERE = Path(__file__).parent
SOURCE_LABELS = {CODED: "Coded", NOTE_MATCH: "Note match", EXPORT: "Export"}
SUPPRESSION_LABELS = {
    "future_appointment": "Visit already booked",
    "chart_do_not_contact": "Do-not-contact on chart",
    "outreach_do_not_contact": "Do-not-contact logged by staff",
    "discharged": "Discharged from care",
    "deceased": "Deceased",
}
DISPOSITION_LABELS = {"reached": "Reached", "booked": "Booked", "declined": "Declined", "no_answer": "No answer",
                      "do_not_contact": "Do not contact"}


def fmt_date(value) -> str:
    if not value:
        return "—"
    if isinstance(value, str):
        value = date.fromisoformat(value[:10])
    return f"{value:%b} {value.day}, {value.year}"


def fmt_time(value: datetime | None) -> str:
    return value.strftime("%b %d, %H:%M UTC") if value else "—"


def create_app(settings: Settings | None = None, session_factory: sessionmaker | None = None,
               http: httpx.Client | None = None) -> FastAPI:
    settings = settings or Settings()
    factory = session_factory or make_session_factory(settings.database_url)
    app = FastAPI(title="Follow-up recall", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.filters["d"] = fmt_date
    templates.env.filters["t"] = fmt_time
    templates.env.filters["n"] = lambda v: f"{v:,}" if isinstance(v, int) else v
    templates.env.globals.update(SOURCE_LABELS=SOURCE_LABELS, SUPPRESSION_LABELS=SUPPRESSION_LABELS,
                                 DISPOSITION_LABELS=DISPOSITION_LABELS, actor=settings.actor)
    state: dict = {}

    def fhir_source() -> FhirSource:
        if "fhir" not in state:
            client = http or httpx.Client(timeout=30)
            key = ensure_keypair(settings.client_key_dir).read_bytes()
            fieldmap = load_fieldmap(settings.fieldmap_path)
            auth = SmartBackendClient(settings.token_url, settings.client_id, key, fieldmap.scopes(), client)
            state["fhir"] = FhirSource(settings.fhir_base_url, auth, client)
        return state["fhir"]

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"nav": name.split(".")[0], **ctx})

    def latest_run(s: Session) -> Run | None:
        return s.scalars(select(Run).where(Run.status == "ok").order_by(desc(Run.id)).limit(1)).first()

    def providers(s: Session, run_id: int) -> dict[str, str]:
        return {p.practitioner_id: p.display_name
                for p in s.scalars(select(PractitionerRow).where(PractitionerRow.run_id == run_id))}

    def latest_dispositions(s: Session) -> dict[str, DispositionRow]:
        out: dict[str, DispositionRow] = {}
        for d in s.scalars(select(DispositionRow).order_by(DispositionRow.id)):
            out[d.patient_id] = d
        return out

    def current_sheet():
        try:
            return load_rulesheet(settings.rules_path), None
        except (RuleSheetError, OSError) as exc:
            return None, str(exc)

    def facts_by_patient(s: Session, run_id: int, patient_id: str | None = None) -> dict[str, list]:
        q = select(FactRow).where(FactRow.run_id == run_id)
        if patient_id:
            q = q.where(FactRow.patient_id == patient_id)
        out: dict[str, list] = defaultdict(list)
        for r in s.scalars(q):
            out[r.patient_id].append(fact_from_row(r))
        return out

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/", response_class=HTMLResponse)
    def overview(request: Request, ran: int | None = None):
        sheet, sheet_error = current_sheet()
        with factory() as s:
            runs = s.scalars(select(Run).order_by(desc(Run.id)).limit(10)).all()
            run = latest_run(s)
            just_ran = s.get(Run, ran) if ran else None
            fieldmap = load_fieldmap(settings.fieldmap_path)
            return render(request, "overview.html", run=run, runs=runs, just_ran=just_ran, sheet=sheet,
                          sheet_error=sheet_error, fieldmap=fieldmap,
                          provider_names=providers(s, run.id) if run else {},
                          rule_labels={r.id: r.label for r in sheet.rules} if sheet else {})

    @app.post("/runs")
    def start_run():
        run_id = run_pipeline(settings, factory, settings.actor, http=http)
        return RedirectResponse(f"/?ran={run_id}", status_code=303)

    def worklist_rows(s: Session, run: Run, provider: str | None, rule: str | None, q: str | None = None):
        stmt = select(FlagRow, PatientRow).join(
            PatientRow, (PatientRow.run_id == FlagRow.run_id) & (PatientRow.patient_id == FlagRow.patient_id)
        ).where(FlagRow.run_id == run.id)
        rows = s.execute(stmt).all()
        evidence_ids = {i for f, _ in rows for i in f.evidence}
        facts = {}
        if evidence_ids:
            facts = {r.id: r for r in s.scalars(select(FactRow).where(FactRow.id.in_(evidence_ids)))}
        counts = Counter(f.provider_ref for f, _ in rows)
        if provider:
            rows = [(f, p) for f, p in rows if f.provider_ref == provider]
        if rule:
            rows = [(f, p) for f, p in rows if f.rule_id == rule]
        if q:
            needle = q.strip().lower()
            rows = [(f, p) for f, p in rows
                    if needle in f"{p.given_name} {p.family_name} {p.patient_id}".lower()]
        rows.sort(key=lambda fp: (fp[0].priority, -fp[0].overdue_days, fp[1].family_name))
        return rows, facts, counts

    @app.get("/worklist", response_class=HTMLResponse)
    def worklist(request: Request, provider: str | None = None, rule: str | None = None, q: str | None = None):
        with factory() as s:
            run = latest_run(s)
            if not run:
                return render(request, "empty.html")
            rows, facts, counts = worklist_rows(s, run, provider, rule, q)
            return render(request, "worklist.html", run=run, rows=rows, facts=facts, counts=counts,
                          total=sum(counts.values()), provider=provider, rule=rule, q=q,
                          provider_names=providers(s, run.id), dispositions=latest_dispositions(s))

    @app.get("/worklist.csv")
    def worklist_csv(provider: str | None = None):
        with factory() as s:
            run = latest_run(s)
            if not run:
                raise HTTPException(404, "no runs yet")
            rows, facts, _ = worklist_rows(s, run, provider, None)
            names = providers(s, run.id)
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["priority", "patient_id", "patient", "phone", "provider", "channel", "reason", "detail",
                        "due_on", "overdue_days", "evidence"])
            for f, p in rows:
                w.writerow([f"P{f.priority}", p.patient_id, f"{p.given_name} {p.family_name}", p.phone,
                            names.get(f.provider_ref, f.provider_ref), f.channel, f.rule_label, f.summary,
                            f.due_on, f.overdue_days, "; ".join(facts[i].source_ref for i in f.evidence if i in facts)])
            s.add(AuditRow(actor=settings.actor, action="worklist_exported", run_id=run.id,
                           resource_count=len(rows), detail=f"provider={provider or 'all'}"))
            s.commit()
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="worklist-run-{run.id}.csv"'})

    @app.get("/patients/{patient_id}", response_class=HTMLResponse)
    def patient(request: Request, patient_id: str):
        sheet, _ = current_sheet()
        with factory() as s:
            run = latest_run(s)
            if not run:
                return render(request, "empty.html")
            p = s.get(PatientRow, (run.id, patient_id))
            if p is None:
                raise HTTPException(404, "patient not in the latest run")
            facts = facts_by_patient(s, run.id, patient_id).get(patient_id, [])
            m = measures(facts, sheet, run.as_of) if sheet else None
            flag = s.scalars(select(FlagRow).where(FlagRow.run_id == run.id, FlagRow.patient_id == patient_id)).first()
            supp = s.scalars(select(SuppressionRow).where(SuppressionRow.run_id == run.id,
                                                          SuppressionRow.patient_id == patient_id)).first()
            dropped = s.scalars(select(DroppedRow).where(DroppedRow.run_id == run.id,
                                                         DroppedRow.patient_id == patient_id)).all()
            by_id = {f.id: f for f in facts}
            evidence = [by_id[i] for i in flag.evidence if i in by_id] if flag else []
            also = [(a, [by_id[i] for i in a["evidence"] if i in by_id]) for a in flag.also] if flag else []
            appts = sorted((f for f in facts if f.kind == APPOINTMENT), key=lambda f: f.on, reverse=True)
            history = s.scalars(select(DispositionRow).where(DispositionRow.patient_id == patient_id)
                                .order_by(desc(DispositionRow.id))).all()
            access = s.scalars(select(AuditRow).where(AuditRow.patient_id == patient_id)
                               .order_by(desc(AuditRow.id)).limit(15)).all()
            return render(request, "patient.html", run=run, p=p, m=m, flag=flag, supp=supp, dropped=dropped,
                          evidence=evidence, also=also, appts=appts, history=history, access=access,
                          provider_name=providers(s, run.id).get(p.provider_ref, p.provider_ref),
                          dispositions=DISPOSITIONS, sheet=sheet)

    @app.post("/patients/{patient_id}/disposition")
    def record_disposition(patient_id: str, outcome: str = Form(...), note: str = Form("")):
        if outcome not in DISPOSITIONS:
            raise HTTPException(400, "unknown outcome")
        with factory() as s:
            run = latest_run(s)
            flag = None
            if run:
                flag = s.scalars(select(FlagRow).where(FlagRow.run_id == run.id,
                                                       FlagRow.patient_id == patient_id)).first()
            s.add(DispositionRow(actor=settings.actor, patient_id=patient_id, run_id=run.id if run else None,
                                 flag_id=flag.id if flag else None, rule_id=flag.rule_id if flag else None,
                                 outcome=outcome, note=note.strip()[:300] or None))
            s.add(AuditRow(actor=settings.actor, action="disposition_recorded", run_id=run.id if run else None,
                           patient_id=patient_id, detail=f"{outcome} (outreach log only; chart not written)"))
            s.commit()
        return RedirectResponse(f"/patients/{patient_id}#outreach", status_code=303)

    def measure_rows(s: Session, run: Run, sheet, include_all: bool):
        names = providers(s, run.id)
        flagged = {f.patient_id: f for f in s.scalars(select(FlagRow).where(FlagRow.run_id == run.id))}
        held = {x.patient_id: x for x in s.scalars(select(SuppressionRow).where(SuppressionRow.run_id == run.id))}
        facts = facts_by_patient(s, run.id)
        rows = []
        for p in s.scalars(select(PatientRow).where(PatientRow.run_id == run.id).order_by(PatientRow.patient_id)):
            m = measures(facts.get(p.patient_id, []), sheet, run.as_of)
            if not include_all and not (m.shockwave or m.magnet or m.injections_12mo or m.not_counted):
                continue
            rows.append((p, m, names.get(p.provider_ref, p.provider_ref), flagged.get(p.patient_id),
                         held.get(p.patient_id)))
        return rows

    @app.get("/measures", response_class=HTMLResponse)
    def measures_page(request: Request, all: int = 0):
        sheet, error = current_sheet()
        with factory() as s:
            run = latest_run(s)
            if not run or not sheet:
                return render(request, "empty.html", error=error)
            rows = measure_rows(s, run, sheet, bool(all))
            return render(request, "measures.html", run=run, rows=rows, show_all=bool(all), sheet=sheet)

    @app.get("/measures.csv")
    def measures_csv(all: int = 0):
        sheet, _ = current_sheet()
        with factory() as s:
            run = latest_run(s)
            if not run or not sheet:
                raise HTTPException(404, "no runs yet")
            rows = measure_rows(s, run, sheet, bool(all))
            buf = io.StringIO()
            w = csv.writer(buf)
            w.writerow(["patient_id", "patient", "provider", "shockwave_sessions", "last_shockwave", "magnet_sessions",
                        "last_magnet", "last_visit", "injections_last_12_months", "last_injection"])
            for p, m, prov, _, _ in rows:
                w.writerow([p.patient_id, f"{p.given_name} {p.family_name}", prov, len(m.shockwave),
                            m.last_shockwave.on if m.last_shockwave else "", len(m.magnet),
                            m.last_magnet.on if m.last_magnet else "", m.last_visit.on if m.last_visit else "",
                            len(m.injections_12mo), m.last_injection.on if m.last_injection else ""])
            s.add(AuditRow(actor=settings.actor, action="measures_exported", run_id=run.id, resource_count=len(rows)))
            s.commit()
        return Response(buf.getvalue(), media_type="text/csv",
                        headers={"Content-Disposition": f'attachment; filename="measures-run-{run.id}.csv"'})

    @app.get("/rules", response_class=HTMLResponse)
    def rules_page(request: Request):
        sheet, error = current_sheet()
        fieldmap = load_fieldmap(settings.fieldmap_path)
        with factory() as s:
            run = latest_run(s)
        described = [(r, describe(r, sheet)) for r in sorted(sheet.rules, key=lambda r: r.priority)] if sheet else []
        return render(request, "rules.html", sheet=sheet, error=error, described=described, run=run,
                      fieldmap=fieldmap, settings=settings)

    @app.get("/audit", response_class=HTMLResponse)
    def audit(request: Request, action: str | None = None, patient: str | None = None):
        with factory() as s:
            q = select(AuditRow).order_by(desc(AuditRow.id))
            if action:
                q = q.where(AuditRow.action == action)
            if patient:
                q = q.where(AuditRow.patient_id == patient.strip())
            rows = s.scalars(q.limit(300)).all()
            actions = s.scalars(select(AuditRow.action).distinct().order_by(AuditRow.action)).all()
            total = s.scalar(select(func.count()).select_from(AuditRow))
            return render(request, "audit.html", rows=rows, actions=actions, action=action, patient=patient,
                          total=total)

    @app.get("/report", response_class=HTMLResponse)
    def report(request: Request):
        sheet, _ = current_sheet()
        with factory() as s:
            run = latest_run(s)
            if not run:
                return render(request, "empty.html")
            flagged_ids = set(s.scalars(select(FlagRow.patient_id).where(FlagRow.run_id == run.id)))
            latest = latest_dispositions(s)
            outcomes = Counter(d.outcome for pid, d in latest.items() if pid in flagged_ids)
            worked = sum(outcomes.values())
            return render(request, "report.html", run=run, outcomes=outcomes, worked=worked,
                          flagged=len(flagged_ids), rule_labels={r.id: r.label for r in sheet.rules} if sheet else {})

    @app.get("/source/export/{appointment_id}", response_class=HTMLResponse)
    def source_export(request: Request, appointment_id: str):
        fieldmap = load_fieldmap(settings.fieldmap_path)
        row = read_export_row(settings.export_path, fieldmap, appointment_id) if settings.export_path.exists() else None
        if row is None:
            raise HTTPException(404, "row not found in the export")
        patient_id = row.get(fieldmap.export_columns["patient_id"])
        with factory() as s:
            s.add(AuditRow(actor=settings.actor, action="source_viewed", patient_id=patient_id,
                           resource_type="Appointment", resource_count=1,
                           detail=f"export:{settings.export_path.name}#{appointment_id}"))
            s.commit()
        return render(request, "source.html", ref=f"export:{settings.export_path.name}#{appointment_id}",
                      body=json.dumps(row, indent=2), note_text=None, patient_id=patient_id, origin="export",
                      error=None)

    @app.get("/source/{resource_type}/{resource_id}", response_class=HTMLResponse)
    def source(request: Request, resource_type: str, resource_id: str):
        try:
            resource = fhir_source().read(resource_type, resource_id)
            error = None
        except (FhirError, TokenError, httpx.HTTPError) as exc:
            resource, error = None, str(exc)
        note_text = None
        patient_id = None
        if resource:
            patient_id = subject_id(resource) or next(
                (a["actor"]["reference"].split("/", 1)[1] for a in resource.get("participant") or []
                 if (a.get("actor") or {}).get("reference", "").startswith("Patient/")), None)
            if resource_type == "DocumentReference":
                parts = []
                for c in resource.get("content") or []:
                    data = (c.get("attachment") or {}).get("data")
                    if data:
                        parts.append(base64.b64decode(data).decode("utf-8", errors="replace"))
                        c["attachment"]["data"] = f"<{len(data)} base64 characters, decoded above>"
                note_text = "\n".join(parts)
        with factory() as s:
            s.add(AuditRow(actor=settings.actor, action="source_viewed", patient_id=patient_id,
                           resource_type=resource_type, resource_count=1 if resource else 0,
                           detail=f"{resource_type}/{resource_id}" + (f" failed: {error}"[:200] if error else "")))
            s.commit()
        return render(request, "source.html", ref=f"{resource_type}/{resource_id}",
                      body=json.dumps(resource, indent=2) if resource else None, note_text=note_text,
                      patient_id=patient_id, origin="fhir", error=error)

    return app


def get_app() -> FastAPI:
    return create_app()
