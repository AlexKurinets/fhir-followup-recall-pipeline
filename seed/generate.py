"""Synthetic practice data: FHIR R4 resources plus a staff-run appointment export.

Every person, phone number, address and plan name here is made up. Phone numbers use the
fictional 555-01xx range. Dates are relative to `as_of`, so the demo always looks current.

The first thirteen patients are scripted: each one exercises a specific rule, suppression or edge
case, and STORY records what the pipeline should conclude about them. The rest are generated from
a fixed random seed with realistic visit patterns and treatment courses.
"""

from __future__ import annotations

import base64
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

CPT = "http://www.ama-assn.org/go/cpt"
PRACTICE = "https://example.com/fhir/CodeSystem/practice-procedure"
SNOMED = "http://snomed.info/sct"
ICD10 = "http://hl7.org/fhir/sid/icd-10-cm"
EXT = "https://example.com/fhir/StructureDefinition/"

PRACTITIONERS = {
    "pr-01": {"prefix": ["Dr."], "given": ["Iris"], "family": "Calloway", "suffix": ["MD"]},
    "pr-02": {"prefix": ["Dr."], "given": ["Tomas"], "family": "Rennick", "suffix": ["DO"]},
    "pr-03": {"prefix": ["Dr."], "given": ["Nadia"], "family": "Fenwick", "suffix": ["MD"]},
    "pr-04": {"prefix": [], "given": ["Owen"], "family": "Hartsell", "suffix": ["DPT"]},
}

CONDITIONS = [
    ("M54.50", "Low back pain, unspecified"),
    ("M54.2", "Cervicalgia"),
    ("M72.2", "Plantar fascial fibromatosis"),
    ("M17.11", "Unilateral primary osteoarthritis, right knee"),
    ("M75.41", "Impingement syndrome of right shoulder"),
    ("M76.61", "Achilles tendinitis, right leg"),
    ("G89.29", "Other chronic pain"),
]
INJECTIONS = [
    ("20610", "Arthrocentesis/injection, major joint", "12130007", "Intra-articular route"),
    ("20611", "Arthrocentesis/injection, major joint, with ultrasound", "12130007", "Intra-articular route"),
    ("20552", "Trigger point injection, 1-2 muscles", "78421000", "Intramuscular route"),
    ("20553", "Trigger point injection, 3+ muscles", "78421000", "Intramuscular route"),
    ("64483", "Transforaminal epidural, lumbar/sacral", "404820008", "Epidural route"),
    ("62323", "Interlaminar epidural, lumbar/sacral", "404820008", "Epidural route"),
]
SHOCKWAVE = [(CPT, "0101T", "ESWT, high energy, musculoskeletal"),
             (PRACTICE, "PX-RSWT", "Radial shockwave session")]
MAGNET = [(PRACTICE, "PX-EMTT", "Magnetotransduction therapy session"), (PRACTICE, "PX-PEMF", "PEMF session")]
STEROIDS = ["Triamcinolone acetonide 40 mg/mL injectable suspension",
            "Methylprednisolone acetate 40 mg/mL injectable suspension"]

FIRST = ["Avery", "Blake", "Camila", "Dorian", "Elsie", "Fenna", "Gael", "Hollis", "Ines", "Jory", "Kaia",
         "Lorcan", "Mireille", "Nico", "Odette", "Pascal", "Quinlan", "Rhea", "Soren", "Tamsin", "Ulric", "Vida",
         "Wren", "Xiomara", "Yannick", "Zelda", "Anouk", "Bram", "Clio", "Dashiell", "Emeric", "Fiora", "Galen",
         "Hesper", "Ivo", "Juniper", "Kasimir", "Liesl", "Matthias", "Nerys"]
LAST = ["Ashgrove", "Brightwater", "Cadwell", "Dunmore", "Ellery", "Farrowby", "Glenholm", "Harrowgate",
        "Ivesdale", "Jessop", "Kittredge", "Larkspur", "Merriweather", "Northcote", "Oakhurst", "Pennywhistle",
        "Quillfeather", "Rookwood", "Stillwell", "Thistlewood", "Underhill", "Vantongeren", "Whitlock",
        "Yarborough", "Zellweger-Moss", "Ambleside", "Birchall", "Coldridge", "Dewhurst", "Eastbrook"]
AREA_CODES = ["212", "718", "347", "516", "914"]
PLANS = ["Sample Health PPO", "Example Care HMO", "Demo Medicare Advantage", "Self-pay"]
EXPORT_STATUS = {"fulfilled": "Checked Out", "noshow": "No Show", "cancelled": "Cancelled", "booked": "Scheduled",
                 "arrived": "Arrived"}

# What the pipeline should conclude about each scripted patient (checked by tests and `verify-demo`).
STORY: dict[str, dict] = {
    "p-0001": {"name": "Marisol Quintero", "flag": "POST_INJECTION_FOLLOWUP", "channel": "sms",
               "injections_12mo": 3, "why": "3 injection days in 12 months; the last one is charted twice "
                                            "(Procedure + MedicationAdministration) and counted once"},
    "p-0002": {"name": "Desmond Achterberg", "flag": "SHOCKWAVE_SERIES_INCOMPLETE", "channel": "call",
               "shockwave": 2, "why": "2 of 4 shockwave sessions, no SMS consent"},
    "p-0003": {"name": "Linnea Okonkwo-Hart", "flag": "SHOCKWAVE_SERIES_INCOMPLETE", "shockwave": 3,
               "note_only_sessions": 1, "why": "third session is only in the note, not coded"},
    "p-0004": {"name": "Gideon Marchetti", "suppressed": "future_appointment", "magnet": 4,
               "why": "magnet series stalled but a visit is already booked"},
    "p-0005": {"name": "Rosalind Fairweather", "flag": "MISSED_NOT_RESCHEDULED",
               "why": "no-show in the appointment export, never rebooked"},
    "p-0006": {"name": "Bartholomew Szabo", "suppressed": "chart_do_not_contact",
               "why": "lapsed, but do-not-contact on the chart"},
    "p-0007": {"name": "Imogen Delacroix-Watts", "flag": "PROVIDER_RETURN_REQUEST",
               "why": "note says 'RTC 4 wks', 52 days ago, no visit since"},
    "p-0008": {"name": "Cornelius Abernathy", "flag": "POST_INJECTION_FOLLOWUP", "also": ["LAPSED_ACTIVE_CARE"],
               "why": "two rules fire; the higher priority one leads"},
    "p-0009": {"name": "Thaddeus Nakamura-Byrne", "dropped": "LAPSED_ACTIVE_CARE",
               "why": "active diagnosis but no visit on record, so no fact to cite; dropped"},
    "p-0010": {"name": "Wilhelmina Osei", "flag": None, "shockwave": 0,
               "why": "note says 'declined shockwave session'; negation is not counted"},
    "p-0011": {"name": "Ambrose Kettleworth", "suppressed": "deceased", "why": "deceased on the chart"},
    "p-0012": {"name": "Esperanza Lindqvist", "suppressed": "discharged", "why": "discharged from care"},
    "p-0013": {"name": "Florian Pettigrew", "flag": None, "injections_12mo": 2,
               "why": "flu shot (IM) is not on the medication list, so it is not counted as an injection"},
}


def stamp(day: date, hour: int = 10, minute: int = 30) -> str:
    return f"{day.isoformat()}T{hour:02d}:{minute:02d}:00-04:00"


def b64(text: str) -> str:
    return base64.b64encode(text.encode("utf-8")).decode("ascii")


@dataclass
class SyntheticPractice:
    as_of: date
    resources: dict[str, list[dict]] = field(default_factory=dict)
    export_rows: list[dict] = field(default_factory=list)
    patient_order: list[str] = field(default_factory=list)
    _seq: dict[str, int] = field(default_factory=dict)
    _owner: dict[str, str] = field(default_factory=dict)
    _demo: dict[str, tuple[str, str, str]] = field(default_factory=dict)   # export-only columns per patient

    def _id(self, prefix: str) -> str:
        self._seq[prefix] = self._seq.get(prefix, 0) + 1
        return f"{prefix}-{self._seq[prefix]:06d}"

    def add(self, resource: dict, owner: str | None = None) -> str:
        self.resources.setdefault(resource["resourceType"], []).append(resource)
        if owner:
            self._owner[f"{resource['resourceType']}/{resource['id']}"] = owner
        return resource["id"]

    def ago(self, days: int) -> date:
        return self.as_of - timedelta(days=days)

    # Builders ---------------------------------------------------------------------------------

    def practitioners(self) -> None:
        for pid, name in PRACTITIONERS.items():
            self.add({"resourceType": "Practitioner", "id": pid, "name": [name], "active": True})

    def patient(self, pid: str, given: str, family: str, provider: str, rng: random.Random, sms: bool,
                dnc: bool = False, care: str = "active", deceased: bool = False) -> None:
        extension = [{"url": EXT + "sms-consent", "valueBoolean": sms},
                     {"url": EXT + "care-status", "valueCode": care}]
        if dnc:
            extension.append({"url": EXT + "do-not-contact", "valueBoolean": True})
        birth = date(rng.randint(1945, 2000), rng.randint(1, 12), rng.randint(1, 28))
        self.add({
            "resourceType": "Patient", "id": pid, "active": True,
            "name": [{"use": "official", "given": [given], "family": family}],
            "telecom": [{"system": "phone", "value": f"({rng.choice(AREA_CODES)}) 555-01{rng.randint(0, 99):02d}",
                         "use": "mobile"},
                        {"system": "email", "value": f"{given.lower()}.{family.lower()}@example.com"}],
            "gender": rng.choice(["female", "male"]),
            "birthDate": birth.isoformat(),
            "address": [{"line": [f"{rng.randint(10, 990)} Example Street"], "city": "Sampleton", "state": "NY"}],
            "generalPractitioner": [{"reference": f"Practitioner/{provider}"}],
            "deceasedBoolean": deceased,
            "extension": extension,
        }, owner=pid)
        self.patient_order.append(pid)
        self._demo[pid] = (f"{given} {family}", birth.isoformat(), rng.choice(PLANS))

    def condition(self, pid: str, code: str, display: str, onset: date, active: bool = True) -> None:
        self.add({
            "resourceType": "Condition", "id": self._id("cond"),
            "clinicalStatus": {"coding": [{"system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                                           "code": "active" if active else "resolved"}]},
            "code": {"coding": [{"system": ICD10, "code": code, "display": display}], "text": display},
            "subject": {"reference": f"Patient/{pid}"},
            "onsetDateTime": onset.isoformat(),
        }, owner=pid)

    def appointment(self, pid: str, day: date, provider: str, status: str, visit_type: str) -> None:
        aid = self._id("appt")
        self.add({
            "resourceType": "Appointment", "id": aid, "status": status,
            "serviceType": [{"text": visit_type}],
            "start": stamp(day, 9 + int(aid[-1]) % 7), "end": stamp(day, 9 + int(aid[-1]) % 7, 59),
            "participant": [{"actor": {"reference": f"Patient/{pid}"}, "status": "accepted"},
                            {"actor": {"reference": f"Practitioner/{provider}"}, "status": "accepted"}],
        }, owner=pid)
        name, dob, plan = self._demo[pid]
        p = PRACTITIONERS[provider]
        self.export_rows.append({
            "Appointment ID": aid.replace("appt-", "A-"), "Patient Account": pid, "Patient Name": name,
            "DOB": dob, "Address": "Example Street, Sampleton NY", "Phone": "555-01xx", "Insurance": plan,
            "Appt Date": day.isoformat(), "Appt Time": f"{9 + int(aid[-1]) % 7:02d}:00",
            "Provider": " ".join([*p["prefix"], *p["given"], p["family"]]), "Visit Type": visit_type,
            "Status": EXPORT_STATUS[status],
        })

    def visit(self, pid: str, day: date, provider: str, visit_type: str, note: str,
              procedures: list[tuple[str, str, str]] = (), meds: list[tuple[str, str, str]] = (),
              appointment: bool = True) -> str:
        eid = self._id("enc")
        self.add({
            "resourceType": "Encounter", "id": eid, "status": "finished",
            "class": {"system": "http://terminology.hl7.org/CodeSystem/v3-ActCode", "code": "AMB"},
            "type": [{"text": visit_type}],
            "subject": {"reference": f"Patient/{pid}"},
            "participant": [{"individual": {"reference": f"Practitioner/{provider}"}}],
            "period": {"start": stamp(day), "end": stamp(day, 11)},
        }, owner=pid)
        self.add({
            "resourceType": "DocumentReference", "id": self._id("doc"), "status": "current",
            "type": {"text": "Progress note"},
            "subject": {"reference": f"Patient/{pid}"},
            "date": stamp(day, 12),
            "content": [{"attachment": {"contentType": "text/plain", "data": b64(note)}}],
            "context": {"encounter": [{"reference": f"Encounter/{eid}"}]},
        }, owner=pid)
        for system, code, display in procedures:
            self.add({
                "resourceType": "Procedure", "id": self._id("proc"), "status": "completed",
                "code": {"coding": [{"system": system, "code": code, "display": display}]},
                "subject": {"reference": f"Patient/{pid}"}, "encounter": {"reference": f"Encounter/{eid}"},
                "performedDateTime": stamp(day),
            }, owner=pid)
        for med_text, route_code, route_display in meds:
            self.add({
                "resourceType": "MedicationAdministration", "id": self._id("medadm"), "status": "completed",
                "medicationCodeableConcept": {"text": med_text},
                "subject": {"reference": f"Patient/{pid}"}, "context": {"reference": f"Encounter/{eid}"},
                "effectiveDateTime": stamp(day),
                "dosage": {"route": {"coding": [{"system": SNOMED, "code": route_code, "display": route_display}]}},
            }, owner=pid)
        if appointment:
            self.appointment(pid, day, provider, "fulfilled", visit_type)
        return eid

    # Bundles ----------------------------------------------------------------------------------

    def transaction_bundles(self, patients_per_bundle: int = 20) -> list[dict]:
        def entry(r: dict) -> dict:
            return {"resource": r, "request": {"method": "PUT", "url": f"{r['resourceType']}/{r['id']}"}}

        bundles = [{"resourceType": "Bundle", "type": "transaction",
                    "entry": [entry(r) for r in self.resources.get("Practitioner", [])]}]
        by_owner: dict[str, list[dict]] = {}
        for rt, items in self.resources.items():
            if rt == "Practitioner":
                continue
            for r in items:
                by_owner.setdefault(self._owner[f"{rt}/{r['id']}"], []).append(r)
        for i in range(0, len(self.patient_order), patients_per_bundle):
            chunk = self.patient_order[i:i + patients_per_bundle]
            bundles.append({"resourceType": "Bundle", "type": "transaction",
                            "entry": [entry(r) for pid in chunk for r in by_owner.get(pid, [])]})
        return bundles

    def counts(self) -> dict[str, int]:
        return {rt: len(items) for rt, items in sorted(self.resources.items())}


# Scripted patients ------------------------------------------------------------------------------

def _story(px: SyntheticPractice, rng: random.Random) -> None:
    a = px.ago
    knee = INJECTIONS[0]

    def inj(code=knee):
        return [(CPT, code[0], code[1])]

    px.patient("p-0001", "Marisol", "Quintero", "pr-01", rng, sms=True)
    px.condition("p-0001", "M17.11", CONDITIONS[3][1], a(430))
    px.visit("p-0001", a(420), "pr-01", "Office visit", "Right knee pain, worse on stairs. X-ray reviewed. "
             "Discussed injection options.")
    for d in (400, 300, 200):
        px.visit("p-0001", a(d), "pr-01", "Injection visit",
                 "Right knee: triamcinolone 40 mg intra-articular injection performed, tolerated well.",
                 procedures=inj())
    px.visit("p-0001", a(44), "pr-01", "Injection visit",
             "Right knee: triamcinolone 40 mg injection performed under ultrasound. Plan: reassess response.",
             procedures=inj(), meds=[(STEROIDS[0], knee[2], knee[3])])

    px.patient("p-0002", "Desmond", "Achterberg", "pr-02", rng, sms=False)
    px.condition("p-0002", "M72.2", CONDITIONS[2][1], a(90))
    px.visit("p-0002", a(60), "pr-02", "Office visit", "Left plantar fasciitis, 4 months. Discussed ESWT, "
             "series of 4 recommended.")
    for n, d in enumerate((40, 33), start=1):
        px.visit("p-0002", a(d), "pr-02", "Shockwave session", f"ESWT session {n} of 4, left heel, 2000 pulses.",
                 procedures=[SHOCKWAVE[0]])

    px.patient("p-0003", "Linnea", "Okonkwo-Hart", "pr-03", rng, sms=True)
    px.condition("p-0003", "M76.61", CONDITIONS[5][1], a(120))
    for n, d in enumerate((50, 43), start=1):
        px.visit("p-0003", a(d), "pr-03", "Shockwave session",
                 f"Radial shockwave session {n}, right Achilles insertion, 2500 pulses.", procedures=[SHOCKWAVE[1]])
    px.visit("p-0003", a(36), "pr-03", "Shockwave session",
             "Radial shockwave session 3 completed, right Achilles insertion, 2500 pulses. Tolerated well.")

    px.patient("p-0004", "Gideon", "Marchetti", "pr-04", rng, sms=True)
    px.condition("p-0004", "M54.50", CONDITIONS[0][1], a(200))
    for d in (30, 26, 22, 18):
        px.visit("p-0004", a(d), "pr-04", "Magnetic therapy", "EMTT session to lumbar paraspinals, 20 minutes.",
                 procedures=[MAGNET[0]])
    px.appointment("p-0004", a(-6), "pr-04", "booked", "Magnetic therapy")

    px.patient("p-0005", "Rosalind", "Fairweather", "pr-01", rng, sms=True)
    px.condition("p-0005", "M54.2", CONDITIONS[1][1], a(150))
    px.visit("p-0005", a(110), "pr-01", "Office visit", "Neck pain with left-sided radiation. Start PT.")
    px.visit("p-0005", a(50), "pr-01", "Office visit", "Improving. Plan: continue home exercise program.")
    px.appointment("p-0005", a(24), "pr-01", "noshow", "Follow-up")

    px.patient("p-0006", "Bartholomew", "Szabo", "pr-02", rng, sms=False, dnc=True)
    px.condition("p-0006", "M54.50", CONDITIONS[0][1], a(400))
    px.visit("p-0006", a(140), "pr-02", "Office visit", "Chronic low back pain, stable on current regimen.")

    px.patient("p-0007", "Imogen", "Delacroix-Watts", "pr-03", rng, sms=True)
    px.condition("p-0007", "M75.41", CONDITIONS[4][1], a(150))
    px.visit("p-0007", a(120), "pr-03", "Office visit", "Right shoulder impingement. Start PT twice weekly.")
    px.visit("p-0007", a(52), "pr-03", "Office visit", "Subacromial impingement improving. Plan: RTC 4 wks "
             "for reassessment.")

    px.patient("p-0008", "Cornelius", "Abernathy", "pr-01", rng, sms=False)
    px.condition("p-0008", "G89.29", CONDITIONS[6][1], a(500))
    px.visit("p-0008", a(300), "pr-01", "Office visit", "Chronic myofascial pain, upper trapezius.")
    px.visit("p-0008", a(160), "pr-01", "Injection visit", "Trigger point injection performed, 4 muscles.",
             procedures=inj(INJECTIONS[3]))

    px.patient("p-0009", "Thaddeus", "Nakamura-Byrne", "pr-02", rng, sms=True)
    px.condition("p-0009", "M17.11", CONDITIONS[3][1], a(250))

    px.patient("p-0010", "Wilhelmina", "Osei", "pr-03", rng, sms=True)
    px.condition("p-0010", "M72.2", CONDITIONS[2][1], a(60))
    px.visit("p-0010", a(20), "pr-03", "Office visit", "Patient declined shockwave session today; will "
             "reconsider. RTC 6 wks.")

    px.patient("p-0011", "Ambrose", "Kettleworth", "pr-04", rng, sms=True, deceased=True)
    px.condition("p-0011", "M54.50", CONDITIONS[0][1], a(400))
    px.visit("p-0011", a(200), "pr-04", "PT session", "Lumbar stabilization program, session 6.")

    px.patient("p-0012", "Esperanza", "Lindqvist", "pr-01", rng, sms=True, care="discharged")
    px.condition("p-0012", "M17.11", CONDITIONS[3][1], a(300))
    px.visit("p-0012", a(60), "pr-01", "Injection visit", "Left knee injection performed.", procedures=inj())

    px.patient("p-0013", "Florian", "Pettigrew", "pr-02", rng, sms=True)
    px.condition("p-0013", "G89.29", CONDITIONS[6][1], a(300))
    px.visit("p-0013", a(150), "pr-02", "Office visit", "Seasonal vaccine given. Neck and shoulder pain.",
             meds=[("Influenza vaccine, inactivated", "78421000", "Intramuscular route")])
    px.visit("p-0013", a(100), "pr-02", "Injection visit", "Trigger point injection performed, 4 muscles.",
             procedures=inj(INJECTIONS[3]))
    px.visit("p-0013", a(70), "pr-02", "Injection visit", "Trigger point injection performed, 4 muscles.",
             procedures=inj(INJECTIONS[3]), meds=[("Lidocaine 1% injection", "78421000", "Intramuscular route")])
    px.visit("p-0013", a(10), "pr-02", "Office visit", "Good relief from last injections. Continue stretching.")


# Generated patients -----------------------------------------------------------------------------

PLAN_LINES = ["Plan: RTC {n} wks.", "Return to clinic in {n} weeks.", "F/u {n} weeks.", "Follow up in 3 months.",
              "Continue home exercise program; PRN.", "Plan: continue current regimen."]
COMPLAINTS = ["Low back pain", "Neck pain", "Right knee pain", "Left heel pain", "Shoulder pain",
              "Achilles pain", "Hip pain"]
STATUS = ["improving", "unchanged", "slightly worse", "much improved", "stable"]
PATTERNS = {"engaged": (0, 25), "drifting": (26, 85), "lapsed": (95, 300), "dormant": (380, 520), "new": (0, 20)}
FUTURE_BOOKED = {"engaged": 0.65, "drifting": 0.25, "lapsed": 0.08, "dormant": 0.02, "new": 0.6}


def _generated(px: SyntheticPractice, rng: random.Random, pid: str) -> None:
    provider = rng.choices(list(PRACTITIONERS), weights=[3, 3, 2, 2])[0]
    deceased = rng.random() < 0.01
    px.patient(pid, rng.choice(FIRST), rng.choice(LAST), provider, rng, sms=rng.random() < 0.6,
               dnc=rng.random() < 0.03, care="discharged" if rng.random() < 0.04 else "active", deceased=deceased)
    for code, display in rng.sample(CONDITIONS, rng.choice([1, 1, 2])):
        px.condition(pid, code, display, px.ago(rng.randint(200, 900)), active=rng.random() < 0.9)

    pattern = rng.choices(list(PATTERNS), weights=[35, 20, 20, 10, 15])[0]
    gap = rng.randint(*PATTERNS[pattern])
    last = px.ago(gap)
    start = last - timedelta(days=rng.randint(5, 40) if pattern == "new" else rng.randint(60, 420))
    days, d = [], start
    while d < last:
        days.append(d)
        d += timedelta(days=rng.randint(14, 45))
    days.append(last)

    pt = provider == "pr-04"
    injection_days = set()
    if not pt and rng.random() < 0.35:
        injection_days = set(rng.sample(days, min(len(days), rng.choice([1, 1, 2, 3]))))
    for day in days:
        complaint = rng.choice(COMPLAINTS)
        plan = rng.choice(PLAN_LINES).format(n=rng.choice([2, 4, 6]))
        if day in injection_days:
            code = rng.choice(INJECTIONS)
            meds = [(rng.choice(STEROIDS), code[2], code[3])] if rng.random() < 0.4 else []
            px.visit(pid, day, provider, "Injection visit", f"{complaint}, {rng.choice(STATUS)}. Injection "
                     f"performed. {plan}", procedures=[(CPT, code[0], code[1])], meds=meds)
        else:
            px.visit(pid, day, provider, "PT session" if pt else "Office visit",
                     f"{complaint}, {rng.choice(STATUS)}. {plan}")

    def course(kind_codes, label, length, step_days, note_fmt):
        first = rng.choice(days)
        system, code, display = rng.choice(kind_codes)
        for n in range(1, length + 1):
            day = first + timedelta(days=step_days * n)
            if day >= px.as_of:
                break
            coded = rng.random() > 0.08
            px.visit(pid, day, provider, label, note_fmt.format(n=n),
                     procedures=[(system, code, display)] if coded else [])

    if not pt and rng.random() < 0.18:
        course(SHOCKWAVE, "Shockwave session", rng.choice([2, 3, 4, 4, 5]), 7, "Shockwave session {n} completed.")
    if (pt or provider == "pr-03") and rng.random() < 0.3:
        course(MAGNET, "Magnetic therapy", rng.choice([4, 6, 8, 8, 10]), 3, "EMTT session {n}, 20 minutes.")

    if rng.random() < 0.12 and gap > 12:
        px.appointment(pid, last + timedelta(days=rng.randint(7, gap - 3)), provider, "noshow", "Follow-up")
    if not deceased and rng.random() < FUTURE_BOOKED[pattern]:
        px.appointment(pid, px.as_of + timedelta(days=rng.randint(2, 30)), provider, "booked", "Follow-up")


def generate(as_of: date, n_patients: int = 240, seed: int = 7) -> SyntheticPractice:
    rng = random.Random(seed)
    px = SyntheticPractice(as_of=as_of)
    px.practitioners()
    _story(px, rng)
    for i in range(len(STORY) + 1, n_patients + 1):
        _generated(px, rng, f"p-{i:04d}")
    return px
