"""Fresh baseline hardware probes, board lifecycle, and bit-mask accounting."""

from contextlib import contextmanager
from dataclasses import dataclass
import logging
import time

import numpy as np
from drambender.api import DDR4Target, open_board

from .programs import hammer_program, initialize_aggressors, initialize_victim, read_victim
from .timing import hammer_timing

LOG = logging.getLogger("characterization")
POPCOUNT = np.array([i.bit_count() for i in range(256)], dtype=np.uint8)


def xor_mask(readback, expected_byte):
    """Byte-ordered XOR mask, with bit 0 the LSB of each received byte."""
    return np.bitwise_xor(np.asarray(readback).view(np.uint8), expected_byte).copy()


def flip_count(mask):
    return int(POPCOUNT[mask].sum(dtype=np.uint64))


@dataclass
class Probe:
    hc: int
    mask: np.ndarray
    host_wall_seconds: float
    hammer_model_ns: float
    total_hammer_acts: int

    @property
    def flips(self):
        return flip_count(self.mask)

    def metadata(self):
        flips, tested_bits = self.flips, self.mask.size * 8
        return {"hc_per_aggressor": self.hc, "flips": flips,
                "tested_bits": tested_bits, "ber": flips / tested_bits,
                "host_wall_seconds": self.host_wall_seconds,
                "hammer_model_ns": self.hammer_model_ns,
                "total_hammer_acts": self.total_hammer_acts}


@dataclass
class TrialResult:
    metadata: dict
    mask: np.ndarray
    probes: list[Probe]


@contextmanager
def hardware_session(config, opener=open_board):
    target = DDR4Target(rank=config.rank)
    with opener(target, pci_bdf=config.pci_bdf, xdma_channel=config.xdma_channel) as board:
        started = time.monotonic()
        try:
            board.full_reset()
            board.set_aref(False)
            LOG.info("Automatic refresh disabled; host gaps are outside the hammer-program ceiling")
            yield board
        finally:
            # Even a failed reset must not prevent an attempt to restore refresh.
            try:
                board.full_reset()
            finally:
                board.set_aref(True)
            LOG.info("FPGA reset; refresh enabled; session host wall time %.3f s", time.monotonic() - started)


class Measurement:
    """One work unit's static programs, reused but executed afresh every probe."""

    def __init__(self, board, config, victim, pattern, steps, mode):
        self.board, self.config = board, config
        self.pattern, self.steps, self.mode = pattern, steps, mode
        self.target = DDR4Target(rank=config.rank)
        self.addresses = config.addresses(victim)
        self.timing = hammer_timing(steps, mode, config.rank)
        self.aggressors = initialize_aggressors(self.target, config.bank, self.addresses["upper"],
                                               self.addresses["lower"], config.aggressor_byte(pattern) * 0x01010101)
        self.victim = initialize_victim(self.target, config.bank, self.addresses["victim"],
                                       pattern * 0x01010101, config.victim_write_recovery_cycles)
        self.read = read_victim(self.target, config.bank, self.addresses["victim"])
        self.readback = np.empty(config.cachelines_per_row * config.words_per_cacheline, dtype=np.uint32)

    def probe(self, hc):
        self.timing.enforce(hc, self.config.duration_limit_ms)
        started = time.monotonic()
        hammer = hammer_program(self.target, self.config.bank, self.addresses["upper"],
                                self.addresses["lower"], self.mode, hc, self.steps)
        self.board.execute([self.aggressors, self.victim, hammer, self.read])
        # A timeout, short receive, or asynchronous protocol error aborts the
        # unit. No mask or DRAM error count is produced from failed transport.
        received = self.board.receive_into(self.readback, timeout=self.config.read_timeout_seconds)
        if received != self.readback.nbytes:
            raise RuntimeError(f"short readback: {received}/{self.readback.nbytes} bytes")
        self.board.synchronize()
        mask = xor_mask(self.readback, self.pattern)
        probe = Probe(hc, mask, time.monotonic() - started, self.timing.duration_ns(hc),
                      hc * self.timing.acts_per_hc)
        LOG.debug("probe HC/aggressor=%d total_ACTs=%d flips=%d modeled_hammer_ms=%.6f host_s=%.6f",
                  hc, probe.total_hammer_acts, probe.flips, probe.hammer_model_ns / 1e6, probe.host_wall_seconds)
        return probe
