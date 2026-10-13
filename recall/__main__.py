"""Command line.

    python -m recall keys            create the client key pair if missing
    python -m recall run             run the pipeline once and print the summary
    python -m recall verify-demo     run once and check the scripted patients came out as expected
"""

from __future__ import annotations

import argparse
import json
import sys

from sqlalchemy import select

from .config import Settings
from .ehr.smart import ensure_keypair
from .pipeline import run_pipeline
from .rules import measures
from .store import DroppedRow, FactRow, FlagRow, Run, SuppressionRow, fact_from_row, make_session_factory


def check_story(session, run_id: int, settings: Settings) -> list[str]:
    """Compare a run's results for the scripted patients with seed.generate.STORY."""
    from seed.generate import STORY

    from .rulesheet import load_rulesheet

    sheet = load_rulesheet(settings.rules_path)
    run = session.get(Run, run_id)
    flags = {f.patient_id: f for f in session.scalars(select(FlagRow).where(FlagRow.run_id == run_id))}
    supp = {x.patient_id: x for x in session.scalars(select(SuppressionRow).where(SuppressionRow.run_id == run_id))}
    dropped = {(d.patient_id, d.rule_id)
               for d in session.scalars(select(DroppedRow).where(DroppedRow.run_id == run_id))}
    problems = []
    for pid, exp in STORY.items():
        facts = [fact_from_row(r) for r in session.scalars(
            select(FactRow).where(FactRow.run_id == run_id, FactRow.patient_id == pid))]
        m = measures(facts, sheet, run.as_of)
        flag = flags.get(pid)
        if "flag" in exp:
            got = flag.rule_id if flag else None
            if got != exp["flag"]:
                problems.append(f"{pid}: expected flag {exp['flag']}, got {got}")
        if "channel" in exp and flag and flag.channel != exp["channel"]:
            problems.append(f"{pid}: expected channel {exp['channel']}, got {flag.channel}")
        if "also" in exp and flag and sorted(a["rule_id"] for a in flag.also) != sorted(exp["also"]):
            got = [a["rule_id"] for a in flag.also]
            problems.append(f"{pid}: expected additional reasons {exp['also']}, got {got}")
        if "suppressed" in exp and (pid not in supp or supp[pid].reason != exp["suppressed"]):
            got = supp[pid].reason if pid in supp else None
            problems.append(f"{pid}: expected suppression {exp['suppressed']}, got {got}")
        if "dropped" in exp and (pid, exp["dropped"]) not in dropped:
            problems.append(f"{pid}: expected {exp['dropped']} to be dropped for lack of a source fact")
        if "injections_12mo" in exp and len(m.injections_12mo) != exp["injections_12mo"]:
            got = len(m.injections_12mo)
            problems.append(f"{pid}: expected {exp['injections_12mo']} injections in 12 months, got {got}")
        if "shockwave" in exp and len(m.shockwave) != exp["shockwave"]:
            problems.append(f"{pid}: expected {exp['shockwave']} shockwave sessions, got {len(m.shockwave)}")
        if "magnet" in exp and len(m.magnet) != exp["magnet"]:
            problems.append(f"{pid}: expected {exp['magnet']} magnet sessions, got {len(m.magnet)}")
        if "note_only_sessions" in exp:
            got = sum(1 for s in m.shockwave if s.from_note_only)
            if got != exp["note_only_sessions"]:
                problems.append(f"{pid}: expected {exp['note_only_sessions']} note-only sessions, got {got}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m recall")
    sub = parser.add_subparsers(dest="command", required=True)
    k = sub.add_parser("keys")
    k.add_argument("--dir", default=None)
    r = sub.add_parser("run")
    r.add_argument("--actor", default=None)
    v = sub.add_parser("verify-demo")
    v.add_argument("--actor", default="verify-demo@example.com")
    args = parser.parse_args(argv)
    settings = Settings()

    if args.command == "keys":
        from pathlib import Path

        path = ensure_keypair(Path(args.dir) if args.dir else settings.client_key_dir)
        print(f"client key ready at {path}")
        return 0

    factory = make_session_factory(settings.database_url)
    run_id = run_pipeline(settings, factory, args.actor or settings.actor)
    with factory() as s:
        run = s.get(Run, run_id)
        print(json.dumps({"run_id": run.id, "status": run.status, "error": run.error, "as_of": str(run.as_of),
                          "coverage": {k: v["source"] for k, v in (run.coverage or {}).items()},
                          "stats": run.stats}, indent=2))
        if run.status != "ok":
            return 1
        if args.command == "verify-demo":
            problems = check_story(s, run_id, settings)
            for p in problems:
                print(f"MISMATCH {p}", file=sys.stderr)
            print("demo patients: all as expected" if not problems else f"{len(problems)} mismatches")
            return 1 if problems else 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
