#!/usr/bin/env python3
"""Verify V2 read/write with the supplied Samsung SA0 row mapping.

Overwrites physical rows 0..31 in all 16 banks of rank 0. This checks mapped
addressing and data integrity; it does not reverse-engineer physical adjacency.
Run with .venv/bin/python local/verify_sa0.py (no sudo).
"""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import socket
import time

import numpy as np

from drambender import builtin_programs
from drambender.api import DDR4Target, open_board
from drambender.rows import Row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="show the mapping without opening hardware")
    parser.add_argument("--output", type=Path, help="write a new JSON verification report")
    args = parser.parse_args()
    config = json.loads(Path(__file__).with_name("u200_samsung_sa0.json").read_text())
    rows = [Row(row, row_mapping=config["row_mapping"]) for row in range(32)]
    banks = range(16)
    row_map = {str(row.physical_id): row.logical_id for row in rows}
    print(f"SA0 physical -> logical rows: {row_map}", flush=True)
    if args.dry_run:
        return 0
    if socket.gethostname().split(".")[0] != config["hostname"]:
        parser.error("this configuration is specific to safari-fpga62")
    if args.output and args.output.exists():
        parser.error(f"refusing to overwrite {args.output}")

    target = DDR4Target(**{key: config[key] for key in (
        "cachelines_per_row", "column_stride", "words_per_cacheline", "rank"
    )})
    programs = builtin_programs.configure(target=target)
    coordinates = [(bank, row) for bank in banks for row in rows]
    reads = [programs.read_row(bank, row.logical_id) for bank, row in coordinates]
    readback = np.empty(target.cachelines_per_row * target.words_per_cacheline, dtype=np.uint32)
    phases = [
        ("zeros", 0x00000000),
        ("ones", 0xFFFFFFFF),
        ("checkerboard-a", 0xAAAAAAAA),
        ("checkerboard-5", 0x55555555),
        ("bank-row-lane-tags", None),
    ]
    report = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "config": config,
        "physical_to_logical_rows": row_map,
        "banks": list(banks),
        "results": [],
        "status": "fail",
        "scope": "rank 0, physical rows 0..31, banks 0..15; supplied SA0 mapping, not adjacency discovery",
    }
    started = time.monotonic()
    exit_code = 1
    try:
        with open_board(target, pci_bdf=config["pci_bdf"], xdma_channel=config["xdma_channel"]) as board:
            board.full_reset()
            board.set_aref(True)
            try:
                for name, constant in phases:
                    print(f"Checking {name}...", flush=True)
                    patterns = [
                        (constant,) * target.words_per_cacheline if constant is not None else tuple(
                            0xA5000000 | (bank << 16) | (row.physical_id << 8) | lane
                            for lane in range(target.words_per_cacheline)
                        )
                        for bank, row in coordinates
                    ]
                    # Tuple arguments specialize the JIT by value. Compile the
                    # four reusable constants, but use the underlying Python
                    # builder for the 512 different, single-use address tags.
                    build_write = programs.write_row if constant is not None else programs.write_row.__wrapped__
                    writes = [
                        build_write(bank, row.logical_id, pattern)
                        for (bank, row), pattern in zip(coordinates, patterns)
                    ]
                    # Write all addresses before reading any back, so distinct
                    # bank/row/lane tags also detect aliasing within this set.
                    board.execute(writes)
                    board.synchronize()
                    board.execute(reads)
                    mismatched_words = 0
                    for (bank, row), pattern in zip(coordinates, patterns):
                        received = board.receive_into(readback, timeout=5.0)
                        if received != readback.nbytes:
                            raise RuntimeError(f"short read: {received}/{readback.nbytes} bytes")
                        expected = np.tile(np.asarray(pattern, dtype=np.uint32), target.cachelines_per_row)
                        mismatch = int(np.count_nonzero(readback != expected))
                        mismatched_words += mismatch
                        if mismatch:
                            print(f"FAIL {name}: bank={bank} physical_row={row.physical_id} "
                                  f"logical_row={row.logical_id} mismatches={mismatch}", flush=True)
                    board.synchronize()
                    result = {
                        "pattern": name,
                        "rows_checked": len(coordinates),
                        "bytes_checked": len(coordinates) * readback.nbytes,
                        "mismatched_words": mismatched_words,
                    }
                    report["results"].append(result)
                    if mismatched_words:
                        raise RuntimeError(f"{name}: {mismatched_words} mismatched words")
                    print(f"PASS {name}: {len(coordinates)} mapped rows, "
                          f"{result['bytes_checked']} bytes, zero mismatches", flush=True)
            finally:
                board.full_reset()
                board.set_aref(True)
        report["status"] = "pass"
        exit_code = 0
    except KeyboardInterrupt:
        report["status"] = "interrupted"
        report["error"] = "KeyboardInterrupt"
        exit_code = 130
    except Exception as exc:
        report["error"] = f"{type(exc).__name__}: {exc}"
        print(f"FAIL: {report['error']}", flush=True)
    finally:
        report["elapsed_seconds"] = time.monotonic() - started
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            with args.output.open("x") as output:
                json.dump(report, output, indent=2)
                output.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
