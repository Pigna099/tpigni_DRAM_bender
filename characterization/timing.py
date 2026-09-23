"""Duration limits derived from the actual emitted program via the V2 VM."""

from dataclasses import asdict, dataclass
from functools import lru_cache
import math

from drambender.api import DDR4Target

from .programs import UINT32_MAX, hammer_program


@dataclass(frozen=True)
class HammerTiming:
    zero_ns: float
    fixed_ns: float
    per_hc_ns: float
    open_interval_ns: float
    pre_to_act_ns: float
    instructions: int
    acts_per_hc: int

    def duration_ns(self, hc):
        if not 0 <= hc <= UINT32_MAX:
            raise ValueError("HC must fit an unsigned 32-bit register")
        return self.zero_ns if hc == 0 else self.fixed_ns + hc * self.per_hc_ns

    def max_hc(self, limit_ms, count_ceiling=None, *, total_act_ceiling=None):
        """Largest HC satisfying duration, per-aggressor, and total-ACT bounds.

        Total ACTs are divided by the number of aggressors before choosing the
        count. Double-sided hammering must complete whole alternating pairs.
        """
        high = UINT32_MAX if count_ceiling is None else min(UINT32_MAX, count_ceiling)
        if total_act_ceiling is not None:
            high = min(high, total_act_ceiling // self.acts_per_hc)
        if limit_ms is not None:
            high = min(high, math.floor((limit_ms * 1e6 - self.fixed_ns) / self.per_hc_ns))
        return max(0, high)

    def enforce(self, hc, limit_ms):
        if limit_ms is not None and self.duration_ns(hc) > limit_ms * 1e6:
            raise ValueError(f"HC={hc} models {self.duration_ns(hc) / 1e6:.6f} ms, "
                             f"exceeding the {limit_ms:g} ms hammer-program limit")

    def as_dict(self):
        return asdict(self)


@lru_cache(maxsize=256)
def hammer_timing(steps, mode, rank=0):
    target = DDR4Target(rank=rank)
    programs = [hammer_program(target, 1, 10, 12, mode, hc, steps) for hc in range(4)]
    runs = [p.dry_run(max_instructions=10000) for p in programs]
    per_hc = runs[2].total_ns - runs[1].total_ns
    if per_hc <= 0 or runs[3].total_ns - runs[2].total_ns != per_hc:
        raise RuntimeError("emitted hammer schedule is no longer affine in HC")
    trace = programs[2].trace_dram_commands(max_instructions=10000)
    if trace.truncated:
        raise RuntimeError("timing trace was truncated")
    open_intervals, closed_intervals = [], []
    act, pre = None, None
    for event in trace.events:
        if event.command == "ACT":
            if pre is not None:
                closed_intervals.append(event.time_ns - pre)
            act = event.time_ns
        elif event.command == "PRE":
            if act is not None:
                open_intervals.append(event.time_ns - act)
                act = None
            pre = event.time_ns
    acts = 2 if mode == "double" else 1
    if act is not None or len(open_intervals) != 2 * acts or len(set(open_intervals)) != 1:
        raise RuntimeError("hammer ACT/PRE schedule, including terminal interval, changed")
    return HammerTiming(runs[0].total_ns, runs[1].total_ns - per_hc, per_hc,
                        open_intervals[0], min(closed_intervals),
                        programs[2].instruction_count, acts)
