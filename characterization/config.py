"""Shared CLI and resolved, serializable experiment configuration."""

import argparse
from dataclasses import asdict, dataclass
import math
from pathlib import Path
import random
import re

from drambender.rows import Row

from .programs import MAX_LEGACY_STEPS, MODES, UINT32_MAX
from .timing import hammer_timing

DEFAULT_SWEEP = (0, 5, 10, 20, 50, 75, 130, 258, 500, 1000, 2322)
DEFAULT_TOTAL_ACT_CEILING = 1_000_000
ROOT = Path(__file__).resolve().parents[1]


def comma_values(value):
    values = value.split(",")
    if not values or any(not v.strip() for v in values):
        raise ValueError("comma-separated lists must not contain empty items")
    return [v.strip() for v in values]


@dataclass(frozen=True)
class Config:
    experiment: str
    module: str
    pci_bdf: str
    xdma_channel: int
    bank: int
    rank: int
    mapping: str
    rows: tuple[int, ...]
    row_count: int | None
    selection: str
    seed: int
    patterns: tuple[int, ...]
    aggressor_pattern: int | None
    repetitions: int
    temperature_label: str
    modes: tuple[str, ...]
    taggon_steps: tuple[int, ...]
    victim_write_recovery_cycles: int
    duration_limit_ms: float | None
    count_ceiling: int | None
    min_hc: int
    hc: int | None
    hc_values: tuple[int, ...]
    read_timeout_seconds: float
    total_act_ceiling: int | None = None
    resolution: int = 10
    cachelines_per_row: int = 128
    words_per_cacheline: int = 16
    column_stride: int = 8
    temperature_source: str = "external label only; not measured"
    temperature_config: str | None = None
    temperature_poll_seconds: float = 1.0
    temperature_safe_target: float = 50.0

    @property
    def unit_count(self):
        hc_count = len(self.hc_values) if self.experiment == "ber" else 1
        return len(self.rows) * len(self.patterns) * len(self.taggon_steps) * len(self.modes) * hc_count

    def as_dict(self):
        # Normalize tuples to JSON lists for exact resume comparison.
        import json
        return json.loads(json.dumps(asdict(self)))

    def addresses(self, victim):
        # DejaVu's naming convention: upper is physical +1, lower is -1.
        return {name: Row(physical, row_mapping=self.mapping).logical_id
                for name, physical in (("victim", victim), ("upper", victim + 1), ("lower", victim - 1))}

    def aggressor_byte(self, pattern):
        return pattern ^ 0xff if self.aggressor_pattern is None else self.aggressor_pattern


def build_parser():
    # Both subcommands inherit these options; experiment-specific arguments
    # stay separate so BER still requires a fixed timing and count.
    p = argparse.ArgumentParser(add_help=False)
    p.add_argument("--module", default="Samsung-U200-C1-SA0")
    p.add_argument("--pci-bdf", default="0000:01:00.0")
    p.add_argument("--xdma-channel", type=int, choices=[0, 1], default=0)
    p.add_argument("--bank", type=int, choices=range(16), default=1)
    p.add_argument("--rank", type=int, choices=[0], default=0, help="verified rank only")
    p.add_argument("--mapping", choices=["SA0", "linear"], default="SA0")
    p.add_argument("--rows", help="explicit comma-separated physical victim rows")
    p.add_argument("--row-count", type=int, help="actual module rows per bank; required for sampling")
    p.add_argument("--sample-count", type=int, default=256)
    p.add_argument("--seed", type=int, default=123)
    p.add_argument("--patterns", default="00,ff,55,aa", help="victim bytes in hex")
    p.add_argument("--aggressor-pattern", help="hex byte; default complements each victim pattern")
    p.add_argument("--repetitions", type=int, default=50)
    p.add_argument("--temperature", "--temperature-label", dest="temperature_label",
                   help="temperature label; with --temperature-config it is derived automatically")
    p.add_argument("--temperature-config", type=Path,
                   help="fixed JSON temperature-controller configuration")
    p.add_argument("--temperature-poll-seconds", type=float, default=1.0,
                   help="temperature monitor polling interval")
    p.add_argument("--temperature-safe-target", type=float, default=50.0,
                   help="controller target used during temperature cleanup")
    p.add_argument("--hammer-modes", default="double", help="comma-separated upper,lower,double")
    p.add_argument("--victim-write-recovery-cycles", type=int, default=4)
    limit = p.add_mutually_exclusive_group()
    limit.add_argument("--duration-limit-ms", type=float, default=60.0)
    limit.add_argument("--no-duration-limit", action="store_true")
    p.add_argument("--read-timeout-seconds", type=float, default=5.0)
    p.add_argument("--output-dir", type=Path, help="run directory; identical invocation resumes it")
    p.add_argument("--plan", "--audit", action="store_true", help="decode programs and show workload without opening hardware")
    p.add_argument("--audit-file", type=Path, help="also save the hardware-free plan as JSON")
    p.add_argument("--verbose", action="store_true", help="log every probe")
    p.add_argument("--progress-seconds", type=float, default=10.0)
    parser = argparse.ArgumentParser(description="DRAM characterization on U200/C1; tAggON is in legacy steps.")
    experiments = parser.add_subparsers(dest="experiment", required=True)
    hcfirst = experiments.add_parser("hcfirst", parents=[p], help="search HCFirst over a legacy-tAggON sweep",
                                    formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    hcfirst.add_argument("--taggon", "--tAggON", default=",".join(map(str, DEFAULT_SWEEP)), help="legacy step sweep, not ns")
    hcfirst.add_argument("--min-hc", type=int, default=1)
    hcfirst.add_argument("--count-ceiling", type=int, help="optional additional upper bound per aggressor")
    hcfirst.add_argument("--total-act-ceiling", type=int, default=DEFAULT_TOTAL_ACT_CEILING,
                        help="maximum total hammer ACTs across all aggressors per search probe")
    ber = experiments.add_parser("ber", parents=[p], help="repeat trials at a fixed legacy tAggON and HC",
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ber.add_argument("--taggon", "--tAggON", type=int, required=True, help="one fixed legacy step value, not ns")
    ber.add_argument("--hc", type=int, help="ACTs per aggressor; zero is a baseline control")
    ber.add_argument("--hc-start", type=int, help="first HC in a BER sweep")
    ber.add_argument("--hc-end", type=int, help="last HC in a BER sweep, inclusive")
    ber.add_argument("--hc-step", type=int, help="step between BER sweep points")
    return parser


def resolve(args):
    experiment = args.experiment
    if not re.fullmatch(r"[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[01][0-9a-fA-F]\.[0-7]", args.pci_bdf):
        raise ValueError("PCI BDF must have the full dddd:bb:ss.f form")
    if args.row_count is not None and not 3 <= args.row_count <= 2**17:
        raise ValueError("row count must be within the 17-bit U200 address range")
    if args.rows is not None:
        rows = tuple(int(v, 0) for v in comma_values(args.rows))
        selection = "explicit physical rows"
    else:
        if args.row_count is None:
            raise ValueError("sampling requires --row-count; module density is not assumed")
        population = range(1, args.row_count - 1)
        if not 1 <= args.sample_count <= len(population):
            raise ValueError("sample count exceeds the available physical rows")
        rows = tuple(random.Random(args.seed).sample(population, args.sample_count))
        selection = "seeded random sample across the physical row range"
    ceiling = args.row_count or 2**17
    if any(not 1 <= r < ceiling - 1 for r in rows):
        raise ValueError("every victim must have both physical neighbors within the row address range")
    def byte(v):
        if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]{2}", v):
            raise ValueError("patterns must be two hex digits (e.g. 00,ff,55,aa)")
        return int(v, 16)
    patterns = tuple(byte(v) for v in comma_values(args.patterns))
    modes = tuple(comma_values(args.hammer_modes))
    steps = tuple(int(v) for v in comma_values(args.taggon)) if experiment == "hcfirst" else (args.taggon,)
    for name, values in (("rows", rows), ("patterns", patterns), ("modes", modes), ("timings", steps)):
        if len(set(values)) != len(values):
            raise ValueError(f"duplicate {name} are not allowed")
    if any(m not in MODES for m in modes):
        raise ValueError("hammer modes must be upper, lower, or double")
    if any(not 0 <= n <= MAX_LEGACY_STEPS for n in steps):
        raise ValueError("tAggON is outside the supported legacy-step range")
    if args.repetitions < 1 or not 0 <= args.victim_write_recovery_cycles <= UINT32_MAX:
        raise ValueError("repetitions must be positive and recovery cycles must fit uint32")
    for name in ("duration_limit_ms", "read_timeout_seconds", "progress_seconds"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    for name in ("temperature_poll_seconds", "temperature_safe_target"):
        value = getattr(args, name)
        if not math.isfinite(value) or value <= 0:
            raise ValueError(f"{name} must be finite and positive")
    temperature_config = None
    temperature = None
    if args.temperature_config is not None:
        from .temperature import load_config
        temperature = load_config(args.temperature_config)
        if temperature.poll_seconds != args.temperature_poll_seconds:
            raise ValueError("temperature poll interval must match the fixed temperature config")
        if temperature.safe_target != args.temperature_safe_target:
            raise ValueError("temperature safe target must match the fixed temperature config")
        temperature_config = str(args.temperature_config.resolve())
    temperature_label = args.temperature_label
    if temperature_label is None:
        temperature_label = "unknown" if temperature is None else f"{temperature.dram_target:g} C"
    duration = None if args.no_duration_limit else args.duration_limit_ms
    count = getattr(args, "count_ceiling", None)
    total_acts = getattr(args, "total_act_ceiling", None)
    minimum = getattr(args, "min_hc", 1)
    hc = getattr(args, "hc", None)
    sweep_values = [getattr(args, name, None) for name in ("hc_start", "hc_end", "hc_step")]
    if experiment == "ber":
        if hc is not None and any(value is not None for value in sweep_values):
            raise ValueError("BER accepts either --hc or the complete --hc-start/--hc-end/--hc-step sweep")
        if hc is None and any(value is None for value in sweep_values):
            raise ValueError("BER requires --hc or the complete --hc-start/--hc-end/--hc-step sweep")
        if hc is not None:
            hc_values = (hc,)
        else:
            start, end, step = sweep_values
            if start < 0 or end < start or step <= 0:
                raise ValueError("BER HC sweep requires 0 <= start <= end and a positive step")
            hc_values = tuple(range(start, end + 1, step))
    else:
        hc_values = ()
    if not 1 <= minimum <= UINT32_MAX or (count is not None and not minimum <= count <= UINT32_MAX):
        raise ValueError("HCFirst bounds must satisfy 1 <= min HC <= count ceiling <= uint32 max")
    if total_acts is not None and not 1 <= total_acts <= 2 * UINT32_MAX:
        raise ValueError("total ACT ceiling must be positive and at most twice uint32 max")
    if hc is not None and not 0 <= hc <= UINT32_MAX:
        raise ValueError("HC must fit uint32, including zero for controls")
    if any(not 0 <= value <= UINT32_MAX for value in hc_values):
        raise ValueError("BER HC values must fit uint32, including zero for controls")
    config = Config(experiment, args.module, args.pci_bdf.lower(), args.xdma_channel, args.bank,
                    args.rank, args.mapping, rows, args.row_count, selection, args.seed, patterns,
                    None if args.aggressor_pattern is None else byte(args.aggressor_pattern),
                    args.repetitions, temperature_label, modes, steps,
                    args.victim_write_recovery_cycles, duration, count, minimum, hc,
                    hc_values, args.read_timeout_seconds, total_act_ceiling=total_acts,
                    temperature_config=temperature_config,
                    temperature_poll_seconds=args.temperature_poll_seconds,
                    temperature_safe_target=args.temperature_safe_target)
    for row in rows:
        if any(not 0 <= r < ceiling for r in config.addresses(row).values()):
            raise ValueError("mapped address is outside the supplied row count")
    for n in steps:
        for mode in modes:
            timing = hammer_timing(n, mode, config.rank)
            for probe_hc in (hc_values if experiment == "ber" else (hc,)):
                if probe_hc is not None:
                    timing.enforce(probe_hc, duration)
            if experiment == "hcfirst" and timing.max_hc(
                    duration, count, total_act_ceiling=total_acts) < minimum:
                raise ValueError(f"search ceilings leave no HCFirst interval for {mode}, tAggON={n}")
    if not args.plan and args.output_dir is None:
        raise ValueError("hardware runs require --output-dir")
    return config
