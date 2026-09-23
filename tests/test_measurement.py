from dataclasses import replace
from types import SimpleNamespace
import unittest

import numpy as np

from characterization.ber_runner import run_unit as ber_unit
from characterization.config import build_parser, resolve
from characterization.hcfirst_runner import run_unit as hcfirst_unit
from characterization.measurement import Measurement, Probe, hardware_session, xor_mask
from characterization.timing import hammer_timing


def config(experiment="ber", *extra):
    args = [experiment, "--rows", "8", "--patterns", "00", "--repetitions", "2", "--taggon", "0", "--plan"]
    if experiment == "ber":
        args += ["--hc", "0"]
    else:
        args += ["--count-ceiling", "100"]
    return resolve(build_parser().parse_args(args + list(extra)))


def threshold_measure(threshold):
    def measure(hc):
        mask = np.zeros(8192, dtype=np.uint8)
        if threshold is not None and hc >= threshold:
            mask[0] = 0x80
            mask[hc % mask.size] |= 1
        return Probe(hc, mask, 0.001, hammer_timing(0, "double").duration_ns(hc), 2 * hc)
    return SimpleNamespace(probe=measure, timing=hammer_timing(0, "double"))


def search_result(measure, minimum, maximum, resolution=10):
    """Exercise one search through the real HCFirst runner."""
    c = replace(config("hcfirst"), min_hc=minimum, count_ceiling=maximum,
                resolution=resolution, repetitions=1)
    return next(hcfirst_unit(measure, c))


def ber_result(measure, hc):
    return next(ber_unit(measure, replace(config(), hc=hc, repetitions=1)))


class FakeBoard:
    def __init__(self):
        self.calls, self.programs, self.expected = [], [], 0
        self.short = False
        self.receive_error = self.sync_error = None

    def __enter__(self):
        self.calls.append("open")
        return self

    def __exit__(self, *args):
        self.calls.append("close")

    def full_reset(self):
        self.calls.append("reset")

    def set_aref(self, enable):
        self.calls.append(("refresh", enable))

    def execute(self, programs):
        self.programs.append(programs)

    def receive_into(self, buf, timeout=None):
        if self.receive_error:
            raise self.receive_error
        buf.fill(self.expected)
        return buf.nbytes - 4 if self.short else buf.nbytes

    def synchronize(self):
        if self.sync_error:
            raise self.sync_error


class MeasurementTests(unittest.TestCase):
    def test_boundary_flips_censoring_and_single_bound(self):
        first = search_result(threshold_measure(1), 1, 100)
        self.assertEqual(first.metadata["hcfirst"], 1)
        self.assertEqual(first.metadata["status"], "left_censored")
        self.assertIsNone(first.metadata["bracket_low"])
        self.assertEqual([p.hc for p in first.probes], [1, 100])
        high = search_result(threshold_measure(100), 1, 100)
        self.assertEqual(high.metadata["hcfirst"], 100)
        censored = search_result(threshold_measure(None), 1, 100)
        self.assertIsNone(censored.metadata["hcfirst"])
        self.assertEqual(censored.metadata["status"], "right_censored")
        self.assertEqual(censored.metadata["bracket_low"], 100)
        self.assertIsNone(censored.metadata["bracket_high"])
        self.assertEqual(len(censored.probes), 2)
        single = search_result(threshold_measure(None), 1, 1)
        self.assertEqual(len(single.probes), 1)

    def test_bisection_resolution_and_mask_at_flipping_count(self):
        for threshold in (2, 10, 50, 73, 99):
            measure = threshold_measure(threshold)
            result = search_result(measure, 1, 100)
            self.assertLessEqual(result.metadata["bracket_high"] - result.metadata["bracket_low"], 10)
            self.assertLess(result.metadata["bracket_low"], threshold)
            self.assertGreaterEqual(result.metadata["hcfirst"], threshold)
            np.testing.assert_array_equal(result.mask, measure.probe(result.metadata["hcfirst"]).mask)
            self.assertEqual(result.metadata["selected_probe_index"],
                             next(i for i, p in enumerate(result.probes) if p.hc == result.metadata["hcfirst"]))

    def test_independent_repetitions(self):
        current = -1
        thresholds = [1, 75, None]
        def measure(hc):
            nonlocal current
            if hc == 1:
                current += 1
            return threshold_measure(thresholds[current]).probe(hc)
        measurement = SimpleNamespace(probe=measure, timing=hammer_timing(0, "double"))
        results = list(hcfirst_unit(measurement, replace(config("hcfirst"), repetitions=3)))
        self.assertEqual(current, 2)
        self.assertEqual([r.metadata["status"] for r in results], ["left_censored", "bracketed", "right_censored"])
        self.assertTrue(all(r.probes[0].hc == 1 for r in results))

    def test_runner_uses_total_act_budget_for_search_bounds(self):
        for mode in ("upper", "lower", "double"):
            c = replace(config("hcfirst"), count_ceiling=None, repetitions=1, modes=(mode,))
            measure = threshold_measure(None)
            measure.timing = hammer_timing(0, mode)
            result = next(hcfirst_unit(measure, c))
            self.assertIsNone(result.metadata["hcfirst"])
            self.assertEqual(result.metadata["search_max"], 500_000 if mode == "double" else 1_000_000)
            self.assertEqual(result.metadata["search_max_total_acts"], 1_000_000)
            self.assertEqual([p.hc for p in result.probes], [1, result.metadata["search_max"]])

    def test_known_xor_and_ber_normalization(self):
        raw = np.full(2048, 0x55555555, dtype=np.uint32)
        raw[0] ^= 0x80000001
        raw[-1] ^= 0xff
        mask = xor_mask(raw, 0x55)
        np.testing.assert_array_equal(mask[:4], [1, 0, 0, 128])
        probe = Probe(10, mask, 0.1, 1128, 20)
        result = ber_result(SimpleNamespace(probe=lambda hc: probe), 10)
        self.assertEqual(result.metadata["flips"], 10)
        self.assertEqual(result.metadata["tested_bits"], 65536)
        self.assertEqual(result.metadata["ber"], 10 / 65536)
        zero = ber_result(threshold_measure(None), 0)
        self.assertEqual(zero.metadata["ber"], 0)
        self.assertEqual(zero.metadata["status"], "control")
        self.assertEqual(len(list(ber_unit(threshold_measure(None), config()))), 2)

    def test_every_probe_reinitializes_both_neighbors_then_victim(self):
        for mode in ("upper", "lower", "double"):
            for pattern in (0, 255, 85, 170):
                board = FakeBoard()
                board.expected = pattern * 0x01010101
                measure = Measurement(board, config(), 8, pattern, 0, mode)
                self.assertEqual(measure.probe(0).flips, 0)
                self.assertEqual(measure.probe(3).flips, 0)
                self.assertEqual(len(board.programs), 2)
                for programs in board.programs:
                    self.assertEqual(len(programs), 4)
                    self.assertEqual([e.row for e in programs[0].trace_dram_commands().events if e.command == "ACT"], [15, 7])
                    self.assertEqual([e.row for e in programs[1].trace_dram_commands().events if e.command == "ACT"], [14])

    def test_transport_errors_never_produce_masks(self):
        for fault in ("short", "receive", "synchronize"):
            board = FakeBoard()
            board.short = fault == "short"
            board.receive_error = TimeoutError("transport") if fault == "receive" else None
            board.sync_error = RuntimeError("protocol") if fault == "synchronize" else None
            with self.assertRaises((RuntimeError, TimeoutError)):
                Measurement(board, config(), 8, 0, 0, "double").probe(1)

    def test_cleanup_on_success_interruption_and_failure(self):
        for error in (None, KeyboardInterrupt(), RuntimeError("broken transport")):
            board = FakeBoard()
            def run():
                with hardware_session(config(), opener=lambda *a, **k: board):
                    if error:
                        raise error
            if error:
                with self.assertRaises(type(error)):
                    run()
            else:
                run()
            self.assertEqual(board.calls, ["open", "reset", ("refresh", False), "reset", ("refresh", True), "close"])

    def test_duration_enforced_before_transport(self):
        board = FakeBoard()
        measure = Measurement(board, config(), 8, 0, 0, "double")
        with self.assertRaises(ValueError):
            measure.probe(555556)
        self.assertEqual(board.programs, [])


if __name__ == "__main__":
    unittest.main()
