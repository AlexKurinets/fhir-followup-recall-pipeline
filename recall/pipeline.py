"""One run: check what the server exposes, read, extract, evaluate, persist, audit."""

from __future__ import annotations

import time
from collections import Counter, defaultdict
from dataclasses import dataclass

import httpx
from sqlalchemy import insert, select
from sqlalchemy.orm import sessionmaker

from .config import Settings
from .ehr.export import ExportError, read_appointment_export
from .ehr.fhir import FhirSource, supported_resources
from .ehr.smart import SmartBackendClient, ensure_keypair
from .extract import Extractor, patient_record, practitioner_name, ref_id, subject_id
from .facts import Fact, PatientRecord
from .fieldmap import FieldMap, load_fieldmap
from .phrases import Matcher, build_matcher
from .rules import evaluate
from .rulesheet import RuleSheet, load_rulesheet
from .store import (
    AuditRow,
    DispositionRow,
    DroppedRow,
    FactRow,
    FlagRow,
    PatientRow,
    PractitionerRow,
    Run,
    SuppressionRow,
    prune_runs,
    utcnow,
)

READ_ORDER = ("Practitioner", "Patient", "Encounter", "Procedure", "MedicationAdministration", "Condition",
              "DocumentReference", "Appointment")
FHIR, EXPORT_SOURCE, UNAVAILABLE = "fhir", "export", "unavailable"


class PipelineError(RuntimeError):
    pass


@dataclass
class RunContext:
    settings: Settings
    sheet: RuleSheet
    fieldmap: FieldMap
    matcher: Matcher
    fhir: FhirSource
    auth: SmartBackendClient


def build_context(settings: Settings, http: httpx.Client | None = None, matcher: Matcher | None = None) -> RunContext:
    sheet = load_rulesheet(settings.rules_path)
    fieldmap = load_fieldmap(settings.fieldmap_path)
    http = http or httpx.Client(timeout=60)
    key = ensure_keypair(settings.client_key_dir).read_bytes()
    auth = SmartBackendClient(settings.token_url, settings.client_id, key, fieldmap.scopes(), http)
    return RunContext(settings, sheet, fieldmap, matcher or build_matcher(settings.matcher, sheet, settings.llm_model),
                      FhirSource(settings.fhir_base_url, auth, http), auth)


def plan_sources(supported: set[str], fieldmap: FieldMap, export_available: bool) -> dict[str, dict]:
    coverage = {}
    for rt in fieldmap.resource_types:
        if rt in supported:
            source = FHIR
        elif rt == "Appointment" and export_available:
            source = EXPORT_SOURCE
        else:
            source = UNAVAILABLE
        coverage[rt] = {"source": source, "elements": fieldmap.elements(rt) if source == FHIR else None}
    return coverage


def _owner(resource_type: str, resource: dict) -> str | None:
    if resource_type == "Appointment":
        for p in resource.get("participant") or []:
            pid = ref_id((p.get("actor") or {}).get("reference"), "Patient")
            if pid:
                return pid
        return None
    return subject_id(resource)


def run_pipeline(settings: Settings, session_factory: sessionmaker, actor: str,
                 http: httpx.Client | None = None, matcher: Matcher | None = None) -> int:
    ctx = build_context(settings, http, matcher)
    as_of = settings.as_of()
    started = time.perf_counter()

    with session_factory() as s:
        run = Run(actor=actor, as_of=as_of, rule_version=ctx.sheet.version, rule_sha256=ctx.sheet.sha256,
                  matcher=ctx.matcher.name if ctx.matcher.name != "claude" else f"claude:{settings.llm_model}")
        s.add(run)
        s.flush()
        s.add(AuditRow(actor=actor, action="run_started", run_id=run.id,
                       detail=f"rules {ctx.sheet.version} ({ctx.sheet.short_hash}), matcher {run.matcher}"))
        s.commit()
        run_id = run.id

    try:
        _execute(ctx, session_factory, run_id, actor, as_of, started)
    except Exception as exc:
        with session_factory() as s:
            run = s.get(Run, run_id)
            run.status, run.error, run.finished_at = "failed", f"{type(exc).__name__}: {exc}"[:2000], utcnow()
            s.add(AuditRow(actor=actor, action="run_failed", run_id=run_id, detail=run.error[:300]))
            s.commit()
    return run_id


def _execute(ctx: RunContext, session_factory: sessionmaker, run_id: int, actor: str, as_of, started: float) -> None:
    settings, sheet, fieldmap = ctx.settings, ctx.sheet, ctx.fieldmap
    supported = supported_resources(ctx.fhir.capability_statement())
    coverage = plan_sources(supported, fieldmap, settings.export_path.exists())
    if coverage["Patient"]["source"] != FHIR:
        raise PipelineError("Patient is not available over FHIR on this server")
    # Ask only for the scopes we can actually use on this server.
    ctx.auth.scopes = " ".join(f"system/{rt}.read" for rt, c in coverage.items() if c["source"] == FHIR)

    extractor = Extractor(sheet, ctx.matcher)
    patients: dict[str, PatientRecord] = {}
    practitioners: dict[str, str] = {}
    facts: list[Fact] = []
    reads: dict[str, Counter] = defaultdict(Counter)
    export_unmapped = 0

    for rt in READ_ORDER:
        source = coverage.get(rt, {}).get("source")
        if source == FHIR:
            for res in ctx.fhir.search(rt, fieldmap.elements(rt)):
                if rt == "Practitioner":
                    practitioners[res["id"]] = practitioner_name(res)
                elif rt == "Patient":
                    patients[res["id"]] = patient_record(res)
                    reads[res["id"]]["Patient"] += 1
                else:
                    owner = _owner(rt, res)
                    if owner:
                        reads[owner][rt] += 1
                    facts.extend(extractor.dispatch(rt, res))
        elif source == EXPORT_SOURCE:
            try:
                export_facts, export_unmapped = read_appointment_export(settings.export_path, fieldmap)
            except ExportError as exc:
                coverage[rt] = {"source": UNAVAILABLE, "elements": None, "error": str(exc)}
                continue
            for f in export_facts:
                reads[f.patient_id]["Appointment (export)"] += 1
            facts.extend(export_facts)

    orphan_facts = [f for f in facts if f.patient_id not in patients]
    facts = [f for f in facts if f.patient_id in patients]
    by_patient: dict[str, list[Fact]] = defaultdict(list)
    for f in facts:
        by_patient[f.patient_id].append(f)

    with session_factory() as s:
        run = s.get(Run, run_id)
        run.coverage = coverage
        token = ctx.auth.current.as_dict() if ctx.auth.current else {}
        run.token = {**token, "token_url": settings.token_url, "client_id": settings.client_id,
                     "tokens_issued": ctx.auth.tokens_issued}
        if ctx.auth.current:
            s.add(AuditRow(actor=actor, action="token_acquired", run_id=run_id,
                           detail=f"{ctx.auth.tokens_issued} token(s); lifetime {token['lifetime_seconds']}s; "
                                  f"scope {token['scope']}"[:300]))

        s.add_all(PractitionerRow(run_id=run_id, practitioner_id=pid, display_name=name)
                  for pid, name in practitioners.items())
        s.add_all(PatientRow(run_id=run_id, **vars(p)) for p in patients.values())
        s.flush()

        fact_ids: dict[int, int] = {}
        if facts:
            rows = [{"run_id": run_id, "patient_id": f.patient_id, "kind": f.kind, "on": f.on, "status": f.status,
                     "code": f.code, "code_system": f.code_system, "display": f.display,
                     "source_type": f.source_type, "source_ref": f.source_ref, "detail": f.detail} for f in facts]
            ids = s.scalars(insert(FactRow).returning(FactRow.id, sort_by_parameter_order=True), rows).all()
            fact_ids = {id(f): row_id for f, row_id in zip(facts, ids, strict=True)}

        outreach_dnc = set(s.scalars(select(DispositionRow.patient_id).where(
            DispositionRow.outcome == "do_not_contact")).all())

        flagged_by_rule: Counter = Counter()
        suppressed: Counter = Counter()
        channels: Counter = Counter()
        dropped_count = 0
        for pid, patient in patients.items():
            outcome = evaluate(patient, by_patient.get(pid, []), sheet, as_of, pid in outreach_dnc)
            for cand in outcome.dropped:
                dropped_count += 1
                s.add(DroppedRow(run_id=run_id, patient_id=pid, rule_id=cand.rule.id, reason=cand.summary[:300]))
            if outcome.suppressed_reason:
                suppressed[outcome.suppressed_reason] += 1
                s.add(SuppressionRow(run_id=run_id, patient_id=pid, reason=outcome.suppressed_reason,
                                     detail=outcome.suppressed_detail[:300],
                                     rules=[{"rule_id": c.rule.id, "label": c.rule.label, "summary": c.summary}
                                            for c in outcome.suppressed_candidates]))
            if outcome.flagged:
                c = outcome.flagged
                flagged_by_rule[c.rule.id] += 1
                channels[outcome.channel] += 1
                s.add(FlagRow(
                    run_id=run_id, patient_id=pid, provider_ref=patient.provider_ref, rule_id=c.rule.id,
                    rule_label=c.rule.label, priority=c.rule.priority, summary=c.summary[:300], due_on=c.due_on,
                    overdue_days=c.overdue_days(as_of), channel=outcome.channel,
                    evidence=[fact_ids[id(f)] for f in c.evidence],
                    also=[{"rule_id": a.rule.id, "label": a.rule.label, "summary": a.summary,
                           "evidence": [fact_ids[id(f)] for f in a.evidence]} for a in outcome.also],
                ))

        audit_rows = []
        for pid, counter in reads.items():
            if pid not in patients:
                continue
            for rt, n in sorted(counter.items()):
                audit_rows.append({"actor": actor, "action": "export_read" if "export" in rt else "fhir_read",
                                   "run_id": run_id, "patient_id": pid, "resource_type": rt.split(" ")[0],
                                   "resource_count": n, "at": utcnow()})
        if audit_rows:
            s.execute(insert(AuditRow), audit_rows)

        flagged_total = sum(flagged_by_rule.values())
        run.stats = {
            "patients_read": len(patients),
            "practitioners_read": len(practitioners),
            "facts": len(facts),
            "facts_by_kind": extractor.stats.by_kind,
            "flagged": flagged_total,
            "flagged_by_rule": dict(flagged_by_rule),
            "suppressed": sum(suppressed.values()),
            "suppressed_by_reason": dict(suppressed),
            "dropped_no_fact": dropped_count,
            "channels": dict(channels),
            "notes_scanned": extractor.stats.notes_scanned,
            "matcher_errors": extractor.stats.matcher_errors,
            "rejected_undated": extractor.stats.rejected_undated,
            "orphan_facts": len(orphan_facts),
            "export_unmapped_status": export_unmapped,
            "fhir_requests": ctx.fhir.requests_made,
            "duration_s": round(time.perf_counter() - started, 2),
        }
        run.status, run.finished_at = "ok", utcnow()
        s.add(AuditRow(actor=actor, action="run_finished", run_id=run_id,
                       detail=f"{len(patients)} patients read; {flagged_total} flagged; "
                              f"{sum(suppressed.values())} suppressed; {dropped_count} dropped (no source fact)"))
        prune_runs(s, settings.retain_runs)
        s.commit()
