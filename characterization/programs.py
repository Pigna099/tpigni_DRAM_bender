"""Flat U200 programs. Addresses here are *already mapped* logical addresses.

Hammer schedule and baseline initialization follow CMU-SAFARI/DejaVu,
commit aa7a72339184bcdbed41aa5bc71328d97b3ff230, dct/src/programs.
See THIRD_PARTY.md and docs/methodology.md for attribution and differences.
No JIT or upstream private APIs: every emitted command is visible here.
"""

from drambender.api import DDR4Target, ProgramBuilder
from drambender.api.program.instructions import ACT, NOP, PRE, RD, WR

UINT32_MAX = 2**32 - 1
MAX_LEGACY_STEPS = UINT32_MAX // 5 + 1
MODES = ("upper", "lower", "double")


def hammer_program(target, bank, upper, lower, mode, hc, steps):
    if mode == "double":
        return double_sided_hammer(target, bank, upper, lower, hc, steps)
    if mode not in MODES:
        raise ValueError(f"unknown hammer mode: {mode}")
    return single_sided_hammer(target, bank, upper if mode == "upper" else lower, hc, steps)


def check_capacity(program, target):
    if program.instruction_count > target.instruction_capacity:
        raise ValueError("program exceeds the U200 instruction capacity")
    return program


def initialize_aggressors(target: DDR4Target, bank: int, upper: int, lower: int, word: int):
    """Initialize both neighbors, upper (+1) then lower (-1), in every mode.

    Preserve DejaVu init_rows' scratchpad loop and command timing.
    """
    p = ProgramBuilder(target=target)
    p.LI(2, "NUM_ROWS")
    p.LI(0, "MEM_PTR")
    for row in (upper, lower):
        p.LI(row, "ROW_ADDR")
        p.ST("MEM_PTR", 0, "ROW_ADDR")
        p.LI(word, "PATTERN_REG")
        p.ST("MEM_PTR", 1, "PATTERN_REG")
        p.ADDI("MEM_PTR", 2, "MEM_PTR")
    p.LI(0, "MEM_PTR")
    p.LI(bank, "BAR")
    p.LI(target.column_stride, "CASR")
    p.DRAM(PRE("BAR"), NOP(), NOP(), NOP())
    p.LI(0, "CAR")
    p.LI(0, "ROW_CNT")
    p.LABEL("WRITE_ROW")
    p.LD("MEM_PTR", 0, "RAR")
    p.LD("MEM_PTR", 1, "PATTERN_REG")
    for lane in range(target.words_per_cacheline):
        p.LDWD("PATTERN_REG", lane)
    p.DRAM(ACT("BAR", "RAR"), NOP(), NOP(), NOP())
    p.LI(0, "CAR")
    p.ADDI("MEM_PTR", 2, "MEM_PTR")
    for _ in range(target.cachelines_per_row):
        p.DRAM(WR("BAR", "CAR", icar=1), NOP(), NOP(), NOP())
        p.DRAM(NOP(), NOP(), NOP(), NOP())
    p.DRAM(NOP(), NOP(), NOP(), NOP())
    p.ADDI("ROW_CNT", 1, "ROW_CNT")
    p.DRAM(PRE("BAR"), NOP(), NOP(), NOP())
    p.BL("ROW_CNT", "NUM_ROWS", "WRITE_ROW")
    return check_capacity(p.conclude(), target)


def initialize_victim(target: DDR4Target, bank: int, victim: int, word: int,
                      write_recovery_cycles: int = 4):
    """One baseline write, DejaVu init_row_long_tWR; fixed recovery per run.

    The parameter is the extra fabric-cycle wait after the last WR word and
    its tCCD NOP, not the complete JEDEC tWR command-to-command interval.
    """
    if not 0 <= write_recovery_cycles <= UINT32_MAX:
        raise ValueError("write recovery must fit an unsigned 32-bit cycle count")
    p = ProgramBuilder(target=target)
    p.LI(bank, "BAR")
    p.LI(victim, "RAR")
    p.LI(target.column_stride, "CASR")
    for lane in range(target.words_per_cacheline):
        p.LI(word, "PATTERN_REG")
        p.LDWD("PATTERN_REG", lane)
    p.DRAM(PRE("BAR"), NOP(), NOP(), NOP())
    p.LI(0, "CAR")
    p.DRAM(NOP(), NOP(), NOP(), NOP())
    p.DRAM(NOP(), NOP(), NOP(), NOP())
    p.DRAM(ACT("BAR", "RAR"), NOP(), NOP(), NOP())
    p.DRAM(NOP(), NOP(), NOP(), NOP())
    p.DRAM(NOP(), NOP(), NOP(), NOP())
    for _ in range(target.cachelines_per_row):
        p.DRAM(WR("BAR", "CAR", icar=1), NOP(), NOP(), NOP())
        p.DRAM(NOP(), NOP(), NOP(), NOP())
    if write_recovery_cycles:
        p.SLEEP(write_recovery_cycles)
    p.DRAM(PRE("BAR"), NOP(), NOP(), NOP())
    p.DRAM(NOP(), NOP(), NOP(), NOP())
    p.DRAM(NOP(), NOP(), NOP(), NOP())
    return check_capacity(p.conclude(), target)


def single_sided_hammer(target: DDR4Target, bank: int, aggressor: int, hc: int, steps: int):
    if not 0 <= hc <= UINT32_MAX or not 0 <= steps <= MAX_LEGACY_STEPS:
        raise ValueError("HC or legacy tAggON exceeds the ISA range")
    p = ProgramBuilder(target=target)
    p.LI(bank, "BAR")
    p.LI(aggressor, "RAR")
    p.LI(0, "HMR_COUNTER")
    p.LI(hc, "NUM_HMR")
    if hc:
        p.LABEL("HAMMER")
        if steps > 1:
            p.SLEEP(5 * (steps - 1))
        p.DRAM(PRE("BAR"), NOP(), NOP(), NOP())
        p.ADDI("HMR_COUNTER", 1, "HMR_COUNTER")
        p.DRAM(NOP(), NOP(), NOP(), ACT("BAR", "RAR"))
        p.BL("HMR_COUNTER", "NUM_HMR", "HAMMER")
        # Intentional departure: complete the last open interval as if the
        # next loop iteration ran, then close it before returning to the host.
        if steps > 1:
            p.SLEEP(5 * (steps - 1))
    p.DRAM(PRE("BAR"), NOP(), NOP(), NOP())
    p.SLEEP(3)
    return check_capacity(p.conclude(), target)


def double_sided_hammer(target: DDR4Target, bank: int, upper: int, lower: int,
                        hc: int, steps: int):
    if not 0 <= hc <= UINT32_MAX or not 0 <= steps <= MAX_LEGACY_STEPS:
        raise ValueError("HC or legacy tAggON exceeds the ISA range")
    p = ProgramBuilder(target=target)
    p.LI(bank, "BAR")
    p.LI(upper, "RAR")
    p.LI(0, "HMR_COUNTER")
    p.LI(hc, "NUM_HMR")
    if hc:
        p.LABEL("HAMMER")
        if steps > 1:
            p.SLEEP(5 * (steps - 1))
        p.DRAM(PRE("BAR"), NOP(), NOP(), NOP())
        p.LI(upper, "RAR")
        p.DRAM(NOP(), NOP(), NOP(), ACT("BAR", "RAR"))
        p.LI(lower, "RAR")
        p.SLEEP(5)
        if steps > 1:
            p.SLEEP(5 * (steps - 1))
        p.DRAM(PRE("BAR"), NOP(), NOP(), NOP())
        p.ADDI("HMR_COUNTER", 1, "HMR_COUNTER")
        p.DRAM(NOP(), NOP(), NOP(), ACT("BAR", "RAR"))
        p.BL("HMR_COUNTER", "NUM_HMR", "HAMMER")
        if steps > 1:
            p.SLEEP(5 * (steps - 1))
    p.DRAM(PRE("BAR"), NOP(), NOP(), NOP())
    p.SLEEP(3)
    return check_capacity(p.conclude(), target)


def read_victim(target: DDR4Target, bank: int, victim: int):
    p = ProgramBuilder(target=target)
    p.LI(bank, "BAR")
    p.LI(victim, "RAR")
    p.LI(target.column_stride, "CASR")
    p.DRAM(PRE("BAR"), NOP(), NOP(), NOP())
    p.LI(0, "CAR")
    p.SLEEP(2)
    p.DRAM(ACT("BAR", "RAR"), NOP(), NOP(), NOP())
    p.SLEEP(2)
    for _ in range(target.cachelines_per_row):
        p.DRAM(RD("BAR", "CAR", icar=1), NOP(), NOP(), NOP())
        p.SLEEP(1)
    p.SLEEP(4)
    p.DRAM(PRE("BAR"), NOP(), NOP(), NOP())
    p.SLEEP(3)
    return check_capacity(p.conclude(), target)
