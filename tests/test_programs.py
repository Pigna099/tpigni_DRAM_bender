import unittest

from drambender.api import DDR4Target

from characterization.config import build_parser, resolve
from characterization.programs import hammer_program, initialize_aggressors, initialize_victim, read_victim, check_capacity
from characterization.timing import hammer_timing


class ProgramTests(unittest.TestCase):
    def setUp(self):
        self.target = DDR4Target()

    def test_hammer_commands_and_terminal_timing(self):
        # Independent expected schedule in U200 command slots, including HC=1
        # and the final PRE. This also guards against accidental branch cost changes.
        for mode in ("upper", "lower", "double"):
            for steps in (0, 1, 2, 5, 2322):
                extra = 30 * max(0, steps - 1)
                for hc in (0, 1, 3, 7):
                    with self.subTest(mode=mode, steps=steps, hc=hc):
                        p = hammer_program(self.target, 1, 15, 7, mode, hc, steps)
                        trace = p.trace_dram_commands(max_instructions=10000)
                        self.assertFalse(trace.truncated)
                        acts = [e for e in trace.events if e.command == "ACT"]
                        expected = ([15, 7] if mode == "double" else [15 if mode == "upper" else 7]) * hc
                        self.assertEqual([e.row for e in acts], expected)
                        self.assertEqual(trace.events[-1].command, "PRE")
                        current, previous_pre = None, None
                        for e in trace.events:
                            self.assertEqual(e.bank, 1)
                            self.assertEqual(e.rank, 0)
                            if e.command == "ACT":
                                self.assertIsNone(current)
                                self.assertEqual(e.slot, 3)
                                self.assertEqual(e.time_ns - previous_pre, 16.5)
                                current = e.time_ns
                            elif e.command == "PRE":
                                if current is not None:
                                    self.assertEqual(e.time_ns - current, 37.5 + extra)
                                    current = None
                                previous_pre = e.time_ns
                        self.assertIsNone(current)
                        expected_duration = 48 if hc == 0 else 48 + extra + hc * (54 + extra) * (2 if mode == "double" else 1)
                        self.assertEqual(trace.total_ns, expected_duration)
                        self.assertLessEqual(p.instruction_count, self.target.instruction_capacity)
                        self.assertEqual(hammer_timing(steps, mode).duration_ns(hc), trace.total_ns)

    def test_limit_is_exact_and_includes_terminal(self):
        for steps in (0, 1, 5, 2322):
            for mode in ("upper", "lower", "double"):
                t = hammer_timing(steps, mode)
                maximum = t.max_hc(60)
                self.assertLessEqual(t.duration_ns(maximum), 60e6)
                self.assertGreater(t.duration_ns(maximum + 1), 60e6)
                t.enforce(maximum, 60)
                with self.assertRaises(ValueError):
                    t.enforce(maximum + 1, 60)
                self.assertEqual(t.max_hc(60, 10), 10)
                t.enforce(maximum + 1, None)
        self.assertEqual(hammer_timing(0, "double"), hammer_timing(1, "double"))
        self.assertEqual(hammer_timing(0, "double").max_hc(60), 555555)

    def test_capacity_guard(self):
        class HugeProgram:
            instruction_count = 32769
        with self.assertRaises(ValueError):
            check_capacity(HugeProgram(), self.target)

    def test_total_act_ceiling_combines_with_duration_and_optional_count(self):
        for mode in ("upper", "lower", "double"):
            for steps in (0, 1, 2, 5, 130, 2322):
                with self.subTest(mode=mode, steps=steps):
                    timing = hammer_timing(steps, mode)
                    maximum = timing.max_hc(60, total_act_ceiling=1_000_000)
                    self.assertLessEqual(maximum * timing.acts_per_hc, 1_000_000)
                    self.assertLessEqual(timing.duration_ns(maximum), 60e6)
                    self.assertTrue((maximum + 1) * timing.acts_per_hc > 1_000_000
                                    or timing.duration_ns(maximum + 1) > 60e6)
                    self.assertEqual(timing.max_hc(60, 10, total_act_ceiling=1_000_000), 10)
        self.assertEqual(hammer_timing(0, "double").max_hc(60, total_act_ceiling=1_000_001), 500_000)

    def test_full_rowhammer_budget_decodes_to_one_million_total_acts(self):
        # Exercise the actual large loop and LI operands, not just the affine
        # duration estimate or a short representative command trace.
        for mode, hc in (("upper", 1_000_000), ("double", 500_000)):
            program = hammer_program(self.target, 1, 15, 7, mode, hc, 0)
            run = program.dry_run(max_instructions=20_000_000)
            self.assertEqual(run.dram_cmd_counts["ACT"], 1_000_000)
            self.assertEqual(run.dram_cmd_counts["PRE"], 1_000_001)
            self.assertEqual(run.total_ns, 54_000_048)

    def test_initialization_mapping_patterns_and_recovery(self):
        c = resolve(build_parser().parse_args(["ber", "--rows", "8", "--hc", "0", "--taggon", "0", "--plan"]))
        self.assertEqual(c.addresses(8), {"victim": 14, "upper": 15, "lower": 7})
        for byte in (0, 255, 85, 170):
            word = byte * 0x01010101
            a = initialize_aggressors(self.target, 1, 15, 7, (byte ^ 255) * 0x01010101)
            v = initialize_victim(self.target, 1, 14, word)
            at = a.trace_dram_commands()
            vt = v.trace_dram_commands()
            self.assertEqual([e.row for e in at.events if e.command == "ACT"], [15, 7])
            self.assertEqual([e.row for e in vt.events if e.command == "ACT"], [14])
            self.assertEqual(sum(e.command == "WR" for e in at.events), 256)
            self.assertEqual(sum(e.command == "WR" for e in vt.events), 128)
            self.assertEqual(str(v).count(f"LI PATTERN_REG, {word}"), 16)
            self.assertEqual(str(a).count(f"LI PATTERN_REG, {(byte ^ 255) * 0x01010101}"), 2)
            self.assertEqual(vt.events[-1].time_ns - [e for e in vt.events if e.command == "WR"][-1].time_ns, 36)
        r = read_victim(self.target, 1, 14).trace_dram_commands()
        self.assertEqual([e.column for e in r.events if e.command == "RD"], list(range(0, 1024, 8)))
        self.assertEqual(r.events[-1].command, "PRE")
        v8 = initialize_victim(self.target, 1, 14, 0, 8).trace_dram_commands()
        self.assertEqual(v8.events[-1].time_ns - [e for e in v8.events if e.command == "WR"][-1].time_ns, 60)


if __name__ == "__main__":
    unittest.main()
