import unittest

from characterization.audit import build_audit
from characterization.config import DEFAULT_SWEEP, build_parser, resolve


class ConfigTests(unittest.TestCase):
    def parse(self, *args, experiment="hcfirst"):
        return resolve(build_parser().parse_args([experiment, "--plan", *args]))

    def test_defaults_and_sampling(self):
        c = self.parse("--row-count", "32768")
        self.assertEqual(c.rows, self.parse("--row-count", "32768").rows)
        self.assertEqual(len(c.rows), 256)
        self.assertTrue(all(1024 <= r < 32768 - 1024 for r in c.rows))
        self.assertEqual(c.taggon_steps, DEFAULT_SWEEP)
        self.assertEqual(c.patterns, (0, 255, 85, 170))
        self.assertEqual(c.modes, ("double",))
        self.assertEqual(c.repetitions, 50)
        self.assertEqual(c.temperature_label, "unknown")
        self.assertEqual(c.unit_count, 256 * 4 * 11)
        self.assertEqual(c.total_act_ceiling, 1_000_000)

    def test_sampling_and_invalid_inputs(self):
        for args in ([], ["--row-count", "2048"], ["--rows", "0"], ["--rows", "1,1"],
                     ["--rows", "8", "--patterns", "gg"], ["--rows", "8", "--hammer-modes", "bogus"],
                     ["--rows", "8", "--taggon", "-1"], ["--rows", "8", "--repetitions", "0"],
                     ["--rows", "8", "--total-act-ceiling", "0"],
                     ["--rows", "8", "--total-act-ceiling", "-1"],
                     ["--rows", "8", "--total-act-ceiling", "1"],
                     ["--rows", "8", "--taggon", "0", "--min-hc", "500001"],
                     ["--rows", "8", "--duration-limit-ms", "nan"],
                     ["--rows", "8", "--duration-limit-ms", "0.000001"]):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.parse(*args)

    def test_search_audit_separates_total_acts_from_per_aggressor_hc(self):
        c = self.parse("--rows", "8", "--taggon", "0,2322", "--hammer-modes", "upper,lower,double")
        points = {(p["legacy_taggon_steps"], p["mode"]): p for p in build_audit(c)["hammer_points"]}
        for mode in ("upper", "lower", "double"):
            rh = points[0, mode]
            self.assertEqual(rh["search_max_hc"], 500_000 if mode == "double" else 1_000_000)
            self.assertEqual(rh["search_max_total_acts"], 1_000_000)
            self.assertEqual(rh["selected_hammer_ms"], 54.000048)
            rp = points[2322, mode]
            self.assertEqual(rp["search_max_hc"], 430 if mode == "double" else 860)
            self.assertEqual(rp["search_max_total_acts"], 860)
            self.assertEqual(rp["selected_hammer_ms"], 59.997918)
        # An explicit duration override still has a finite search ACT budget.
        c = self.parse("--rows", "8", "--taggon", "0", "--no-duration-limit")
        self.assertEqual(build_audit(c)["hammer_points"][0]["search_max_hc"], 500_000)

    def test_fixed_ber_duration_control_and_explicit_disable(self):
        args = ["--rows", "8", "--taggon", "0", "--hc", "555556"]
        with self.assertRaises(ValueError):
            self.parse(*args, experiment="ber")
        self.parse(*args, "--duration-limit-ms", "61", experiment="ber")
        self.parse(*args, "--no-duration-limit", experiment="ber")
        c = self.parse("--rows", "8", "--taggon", "0", "--hc", "0", experiment="ber")
        audit = build_audit(c)
        self.assertFalse(audit["hardware_opened"])
        self.assertEqual(audit["workload"]["minimum_probes"], c.unit_count * c.repetitions)
        self.assertEqual(audit["hammer_points"][0]["selected_hammer_ms"], 0.000048)
        self.assertEqual(audit["Temperature"], "unknown")

    def test_temperature_alias_and_audit(self):
        for experiment in ("hcfirst", "ber"):
            args = ["--rows", "8", "--taggon", "0"]
            if experiment == "ber":
                args += ["--hc", "0"]
            c = self.parse(*args, "--temperature", "50 C", experiment=experiment)
            old_option = self.parse(*args, "--temperature-label", "50 C", experiment=experiment)
            self.assertEqual(c, old_option)
            self.assertEqual(build_audit(c)["Temperature"], "50 C")


if __name__ == "__main__":
    unittest.main()
