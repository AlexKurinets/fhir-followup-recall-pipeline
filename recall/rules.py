"""Deterministic rule evaluation and per-patient measures.

Everything here is plain date arithmetic over Facts. No model is involved: the only model-assisted
step in the pipeline is phrase matching, which happens earlier and produces Facts like any other.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .facts import (
    APPOINTMENT,
    BOOKED_STATUSES,
    CODED,
    CONDITION,
    EXPORT,
    FUTURE_STATUSES,
    NOT_COUNTED,
    NOTE_MATCH,
    RETURN_REQUEST,
    VISIT,
    Fact,
    PatientRecord,
)
from .rulesheet import Rule, RuleSheet

SOURCE_PREFERENCE = {CODED: 0, EXPORT: 1, NOTE_MATCH: 2}


def _d(day: date) -> str:
    return f"{day:%b} {day.day}, {day.year}"


@dataclass(frozen=True)
class Session:
    """One treatment day. Several facts can document the same session; they are kept together."""

    on: date
    facts: tuple[Fact, ...]

    @property
    def primary(self) -> Fact:
        return min(self.facts, key=lambda f: (SOURCE_PREFERENCE.get(f.source_type, 9), f.source_ref or ""))

    @property
    def from_note_only(self) -> bool:
        return all(f.source_type == NOTE_MATCH for f in self.facts)


def sessions(facts: list[Fact], kind: str) -> list[Session]:
    by_day: dict[date, list[Fact]] = defaultdict(list)
    for f in facts:
        if f.kind == kind and f.is_citable:
            by_day[f.on].append(f)
    return [Session(day, tuple(by_day[day])) for day in sorted(by_day)]


def visits(facts: list[Fact]) -> list[Fact]:
    return sorted((f for f in facts if f.kind == VISIT and f.is_citable), key=lambda f: f.on)


@dataclass(frozen=True)
class Measures:
    """The four answers the practice asked for, each backed by the sessions or visit behind it."""

    shockwave: list[Session]
    magnet: list[Session]
    injections_12mo: list[Session]
    last_injection: Session | None
    last_visit: Fact | None
    not_counted: list[Fact] = field(default_factory=list)   # near misses inside the same windows, newest first

    def not_counted_for(self, modality: str) -> list[Fact]:
        return [f for f in self.not_counted if f.code == modality]

    @property
    def last_shockwave(self) -> Session | None:
        return self.shockwave[-1] if self.shockwave else None

    @property
    def last_magnet(self) -> Session | None:
        return self.magnet[-1] if self.magnet else None


def measures(facts: list[Fact], sheet: RuleSheet, as_of: date) -> Measures:
    window_start = as_of - timedelta(days=sheet.lookback_days)
    injections = sessions(facts, "injection")
    past = [s for s in injections if s.on <= as_of]
    v = [f for f in visits(facts) if f.on <= as_of]
    counted = {
        "shockwave": [s for s in sessions(facts, "shockwave") if s.on <= as_of],
        "magnet": [s for s in sessions(facts, "magnet") if s.on <= as_of],
        "injection": [s for s in past if window_start < s.on],
    }
    # A near miss on a day that already counts changes nothing, so it isn't shown.
    counted_days = {(kind, s.on) for kind, ss in counted.items() for s in ss}
    not_counted = sorted(
        (f for f in facts if f.kind == NOT_COUNTED and f.is_citable and f.on <= as_of
         and (f.code, f.on) not in counted_days and (f.code != "injection" or window_start < f.on)),
        key=lambda f: (f.on, f.source_ref or ""), reverse=True)
    return Measures(
        shockwave=counted["shockwave"],
        magnet=counted["magnet"],
        injections_12mo=counted["injection"],
        last_injection=past[-1] if past else None,
        last_visit=v[-1] if v else None,
        not_counted=not_counted,
    )


@dataclass
class Candidate:
    rule: Rule
    evidence: list[Fact]
    due_on: date | None
    summary: str

    def overdue_days(self, as_of: date) -> int:
        return (as_of - self.due_on).days if self.due_on else 0


@dataclass
class Outcome:
    patient: PatientRecord
    flagged: Candidate | None = None
    also: list[Candidate] = field(default_factory=list)
    suppressed_reason: str | None = None
    suppressed_detail: str | None = None
    suppressed_candidates: list[Candidate] = field(default_factory=list)
    dropped: list[Candidate] = field(default_factory=list)
    channel: str | None = None


def _visit_after(facts: list[Fact], day: date) -> Fact | None:
    return next((f for f in visits(facts) if f.on > day), None)


def _eval_post_event(rule: Rule, sheet: RuleSheet, facts: list[Fact], as_of: date) -> Candidate | None:
    past = [s for s in sessions(facts, rule["modality"]) if s.on <= as_of]
    if not past:
        return None
    last = past[-1]
    age = (as_of - last.on).days
    if not (rule["due_after_days"] <= age <= rule["stale_after_days"]):
        return None
    if _visit_after(facts, last.on):
        return None
    label = sheet.modalities[rule["modality"]].label.lower()
    return Candidate(rule, list(last.facts), last.on + timedelta(days=rule["due_after_days"]),
                     f"Last {label} {_d(last.on)} ({age} days ago), no visit since")


def _eval_return_request(rule: Rule, sheet: RuleSheet, facts: list[Fact], as_of: date) -> Candidate | None:
    requests = sorted((f for f in facts if f.kind == RETURN_REQUEST and f.is_citable and f.on <= as_of),
                      key=lambda f: f.on)
    if not requests:
        return None
    note = requests[-1]
    interval = next((r.interval_days for r in sheet.return_requests if r.id == note.code), None)
    if interval is None:
        return None
    due = note.on + timedelta(days=interval + rule["grace_days"])
    if as_of < due or (as_of - note.on).days > rule["stale_after_days"]:
        return None
    if _visit_after(facts, note.on):
        return None
    return Candidate(rule, [note], due,
                     f"Note on {_d(note.on)} asked for a return in {interval} days; no visit since")


def _eval_series(rule: Rule, sheet: RuleSheet, facts: list[Fact], as_of: date) -> Candidate | None:
    window_start = as_of - timedelta(days=rule["series_window_days"])
    recent = [s for s in sessions(facts, rule["modality"]) if window_start <= s.on <= as_of]
    if not recent or len(recent) >= rule["series_target"]:
        return None
    last = recent[-1]
    if (as_of - last.on).days < rule["due_after_days"]:
        return None
    label = sheet.modalities[rule["modality"]].label.lower()
    return Candidate(rule, [f for s in recent for f in s.facts], last.on + timedelta(days=rule["due_after_days"]),
                     f"{len(recent)} of {rule['series_target']} {label}s; last {_d(last.on)}")


def _eval_missed(rule: Rule, sheet: RuleSheet, facts: list[Fact], as_of: date) -> Candidate | None:
    appts = [f for f in facts if f.kind == APPOINTMENT and f.is_citable]
    window_start = as_of - timedelta(days=rule["lookback_days"])
    missed = sorted((f for f in appts if f.status == "noshow" and window_start <= f.on <= as_of), key=lambda f: f.on)
    if not missed:
        return None
    last = missed[-1]
    if (as_of - last.on).days < rule["due_after_days"]:
        return None
    if _visit_after(facts, last.on):
        return None
    if any(f.on > last.on and f.status in BOOKED_STATUSES for f in appts):
        return None
    return Candidate(rule, [last], last.on + timedelta(days=rule["due_after_days"]),
                     f"No-show {_d(last.on)}, not rescheduled")


def _eval_lapsed(rule: Rule, sheet: RuleSheet, facts: list[Fact], as_of: date) -> Candidate | None:
    conditions = [f for f in facts if f.kind == CONDITION and f.status == "active"
                  and any((f.code or "").startswith(p) for p in rule["condition_code_prefixes"])]
    if not conditions:
        return None
    past_visits = [f for f in visits(facts) if f.on <= as_of]
    if not past_visits:
        # The rule's question ("how long since the last visit?") has no answer on this chart. The
        # candidate is returned with no evidence, so the pipeline drops it and counts it.
        return Candidate(rule, [], None, "Active diagnosis but no visit on record to measure from")
    last = past_visits[-1]
    age = (as_of - last.on).days
    if not (rule["due_after_days"] <= age <= rule["stale_after_days"]):
        return None
    return Candidate(rule, [last, conditions[0]], last.on + timedelta(days=rule["due_after_days"]),
                     f"Last visit {_d(last.on)} ({age} days ago), active {conditions[0].code}")


EVALUATORS = {
    "post_event_followup": _eval_post_event,
    "return_request": _eval_return_request,
    "series_incomplete": _eval_series,
    "missed_visit": _eval_missed,
    "lapsed": _eval_lapsed,
}


def future_appointment(facts: list[Fact], as_of: date) -> Fact | None:
    upcoming = sorted((f for f in facts if f.kind == APPOINTMENT and f.is_citable and f.on > as_of
                       and f.status in FUTURE_STATUSES), key=lambda f: f.on)
    return upcoming[0] if upcoming else None


def evaluate(patient: PatientRecord, facts: list[Fact], sheet: RuleSheet, as_of: date,
             outreach_do_not_contact: bool = False) -> Outcome:
    outcome = Outcome(patient=patient)
    candidates: list[Candidate] = []
    for rule in sheet.rules:
        cand = EVALUATORS[rule.kind](rule, sheet, facts, as_of)
        if cand is None:
            continue
        # A flag must cite at least one dated fact with a source reference. Anything else is dropped.
        if not cand.evidence or not all(f.is_citable for f in cand.evidence):
            outcome.dropped.append(cand)
            continue
        candidates.append(cand)
    if not candidates:
        return outcome

    order = {r.id: i for i, r in enumerate(sheet.rules)}
    candidates.sort(key=lambda c: (c.rule.priority, order[c.rule.id]))

    s = sheet.suppress_when
    upcoming = future_appointment(facts, as_of)
    reason = detail = None
    if s["deceased"] and patient.deceased:
        reason, detail = "deceased", "Deceased on the chart"
    elif s["chart_do_not_contact"] and patient.chart_do_not_contact:
        reason, detail = "chart_do_not_contact", "Do-not-contact on the chart"
    elif s["outreach_do_not_contact"] and outreach_do_not_contact:
        reason, detail = "outreach_do_not_contact", "Staff logged do-not-contact"
    elif s["discharged"] and patient.care_status == "discharged":
        reason, detail = "discharged", "Discharged from care"
    elif s["future_appointment"] and upcoming is not None:
        reason, detail = "future_appointment", f"Booked {_d(upcoming.on)} ({upcoming.source_ref})"

    if reason:
        outcome.suppressed_reason, outcome.suppressed_detail = reason, detail
        outcome.suppressed_candidates = candidates
        return outcome

    outcome.flagged, outcome.also = candidates[0], candidates[1:]
    can_text = bool(patient.phone) and (patient.sms_consent or not sheet.sms_requires_consent)
    outcome.channel = "sms" if can_text else "call"
    return outcome
