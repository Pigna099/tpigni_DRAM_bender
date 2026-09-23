#!/usr/bin/env python3
"""Bounded real-FPGA interruption/resume pilot on previously verified rows 8,16.

Run with .venv/bin/python; the characterization package must be installed.
Creates a fresh run directory, sends SIGTERM during its second work unit,
removes the derivative CSV, resumes, and verifies every record and archive.
"""

import argparse
import csv
import json
from pathlib import Path
import signal
import subprocess
import sys
import time

from characterization.checkpoint import sha256


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    directory = args.output_dir.resolve()
    if directory.exists():
        parser.error("interruption pilot requires a fresh output directory")
    root = Path(__file__).resolve().parents[1]
    command = [sys.executable, str(root / "main.py"), "ber", "--module", "Samsung-U200-C1-SA0",
               "--rows", "8,16", "--patterns", "00", "--hammer-modes", "double",
               "--repetitions", "50", "--taggon", "5", "--hc", "1000", "--verbose",
               "--output-dir", str(directory)]
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
    output, in_second, sent = [], False, False
    started = time.monotonic()
    try:
        for line in process.stdout:
            output.append(line)
            if "starting unit r000016" in line:
                in_second = True
            if in_second and "probe HC/aggressor=" in line and not sent:
                process.send_signal(signal.SIGTERM)
                sent = True
            if time.monotonic() - started > 60:
                process.send_signal(signal.SIGTERM)
                raise RuntimeError("interruption pilot exceeded its host time budget")
        code = process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            process.wait(timeout=10)
    if code != 130 or not sent or not any("FPGA reset; refresh enabled" in line for line in output):
        raise RuntimeError(f"interruption/cleanup failed: returncode={code}, signal_sent={sent}")
    before = {p.name: sha256(p) for p in (directory / "units").glob("*.npz")}
    if len(before) != 1:
        raise RuntimeError(f"expected one complete archive before resume, got {len(before)}")
    with (directory / "summary.csv").open() as f:
        before_rows = len(list(csv.DictReader(f)))
    (directory / "summary.csv").unlink()  # force reconstruction from committed NPZ
    resumed = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if resumed.returncode:
        raise RuntimeError(resumed.stderr)
    after = {p.name: sha256(p) for p in (directory / "units").glob("*.npz")}
    if len(after) != 2 or any(after.get(k) != v for k, v in before.items()):
        raise RuntimeError("resume changed a completed unit or failed to commit the interrupted unit")
    with (directory / "summary.csv").open() as f:
        rows = list(csv.DictReader(f))
    keys = {(r["unit_id"], r["repetition"]) for r in rows}
    if len(rows) != 100 or len(keys) != 100:
        raise RuntimeError("resume lost or duplicated result rows")
    complete = subprocess.run(command, capture_output=True, text=True, timeout=60)
    if complete.returncode or "no hardware opened" not in complete.stderr:
        raise RuntimeError("a fully committed resume should rebuild CSV without opening hardware")
    report = {"status": "pass", "command": command, "interrupted_returncode": code,
              "interrupted_during": "second unit after its first probe",
              "refresh_restored_on_interrupt": True, "archives_before_resume": before,
              "archives_after_resume": after, "csv_rows_before_resume": before_rows,
              "csv_removed_before_resume": True, "csv_rows_after_resume": len(rows),
              "unique_result_keys": len(keys), "complete_resume_opened_hardware": False,
              "host_wall_seconds": time.monotonic() - started}
    (directory / "recovery_report.json").write_text(json.dumps(report, indent=2) + "\n")
    (directory / "interruption.log").write_text("".join(output))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
