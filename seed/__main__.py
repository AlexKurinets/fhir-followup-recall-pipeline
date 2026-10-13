"""Seed the FHIR server and write the appointment export.

    python -m seed load --fhir http://fhir:8080/fhir --export-dir /data/exports
    python -m seed summary
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

import httpx

from .generate import STORY, generate


def wait_for(fhir: str, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while True:
        try:
            if httpx.get(f"{fhir}/metadata", timeout=10).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        if time.monotonic() > deadline:
            sys.exit(f"FHIR server at {fhir} did not come up within {timeout:.0f}s")
        time.sleep(3)


def write_export(rows: list[dict], directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "appointments.csv"
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, path)
    return path


def load(args: argparse.Namespace) -> None:
    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    px = generate(as_of, n_patients=args.patients)
    print(f"generated {len(px.patient_order)} patients as of {as_of}: {json.dumps(px.counts())}", flush=True)
    wait_for(args.fhir, args.wait)
    with httpx.Client(timeout=300) as http:
        for i, bundle in enumerate(px.transaction_bundles(), start=1):
            response = http.post(args.fhir, json=bundle, headers={"Content-Type": "application/fhir+json",
                                                                  "Prefer": "return=minimal"})
            if response.status_code >= 300:
                sys.exit(f"bundle {i} failed with {response.status_code}: {response.text[:500]}")
            print(f"bundle {i}: {len(bundle['entry'])} resources loaded", flush=True)
    path = write_export(px.export_rows, Path(args.export_dir))
    print(f"wrote {len(px.export_rows)} appointment rows to {path}", flush=True)


def summary(args: argparse.Namespace) -> None:
    for pid, story in STORY.items():
        print(f"{pid}  {story['name']:<26} {story['why']}")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m seed")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("load", help="load synthetic data into a FHIR server and write the export")
    p.add_argument("--fhir", default=os.environ.get("SEED_FHIR_URL", "http://localhost:8090/fhir"))
    p.add_argument("--export-dir", default=os.environ.get("EXPORT_DIR", "data/exports"))
    p.add_argument("--patients", type=int, default=240)
    p.add_argument("--as-of", default=os.environ.get("AS_OF", ""))
    p.add_argument("--wait", type=float, default=300)
    p.set_defaults(func=load)
    s = sub.add_parser("summary", help="print the scripted demo patients")
    s.set_defaults(func=summary)
    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
