from contextlib import redirect_stderr, redirect_stdout
import csv
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import main
from characterization.measurement import hardware_session
from test_measurement import FakeBoard


class CLITests(unittest.TestCase):
    def test_subcommand_help_and_required_options(self):
        for args in (["--help"], ["hcfirst", "--help"], ["ber", "--help"]):
            with self.subTest(args=args), redirect_stdout(io.StringIO()) as output:
                with self.assertRaises(SystemExit) as error:
                    main.main(args)
                self.assertEqual(error.exception.code, 0)
                self.assertIn(args[0] if args[0] != "--help" else "hcfirst", output.getvalue())
        for args in ([], ["unknown"], ["ber", "--rows", "8", "--plan"],
                     ["ber", "--rows", "8", "--taggon", "0", "--plan"],
                     ["hcfirst", "--rows", "8", "--hc", "0", "--plan"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    main.main(args)
                self.assertEqual(error.exception.code, 2)

    def test_plans_save_matching_json_without_hardware(self):
        with tempfile.TemporaryDirectory() as directory:
            for experiment in ("hcfirst", "ber"):
                path = Path(directory) / f"{experiment}.json"
                args = [experiment, "--rows", "8", "--taggon", "0", "--temperature", "50 C",
                        "--plan", "--audit-file", str(path)]
                if experiment == "ber":
                    args += ["--hc", "0"]
                with self.subTest(experiment=experiment), redirect_stdout(io.StringIO()) as output, \
                     patch("main.hardware_session", side_effect=AssertionError("must not open hardware")):
                    self.assertEqual(main.main(args), 0)
                audit = json.loads(output.getvalue())
                self.assertEqual(audit, json.loads(path.read_text()))
                self.assertFalse(audit["hardware_opened"])
                self.assertEqual(audit["configuration"]["experiment"], experiment)
                self.assertEqual(audit["Temperature"], "50 C")

    def test_main_dispatches_both_experiments_and_resumes_committed_units(self):
        # Run the complete CLI/config/measurement/checkpoint path with a board
        # that returns clean data. The actual programs and runners are used.
        with tempfile.TemporaryDirectory() as directory:
            for experiment in ("hcfirst", "ber"):
                output = Path(directory) / experiment
                args = [experiment, "--rows", "8", "--patterns", "00", "--repetitions", "2",
                        "--taggon", "0", "--temperature", "50 C", "--output-dir", str(output)]
                if experiment == "ber":
                    args += ["--hc", "0"]
                board = FakeBoard()
                def session(config):
                    return hardware_session(config, opener=lambda *a, **k: board)
                with self.subTest(experiment=experiment), \
                     patch("main.hardware_session", side_effect=session), \
                     patch("characterization.checkpoint.provenance", return_value={"test_source": 1}), \
                     patch("main.build_audit", side_effect=AssertionError("audit was not requested")):
                    self.assertEqual(main.main(args), 0)
                    self.assertEqual(board.calls, ["open", "reset", ("refresh", False),
                                                   "reset", ("refresh", True), "close"])
                    self.assertEqual(len(board.programs), 4 if experiment == "hcfirst" else 2)
                    (output / "summary.csv").unlink()
                    with patch("main.hardware_session", side_effect=AssertionError("already complete")):
                        self.assertEqual(main.main(args), 0)
                with (output / "summary.csv").open() as f:
                    rows = list(csv.DictReader(f))
                self.assertEqual(len(rows), 2)
                for row in rows:
                    self.assertEqual(row["experiment"], experiment)
                    self.assertEqual(row["Temperature"], "50 C")
                    self.assertEqual(row["dram_temperature"], "")
                    self.assertEqual(row["liquid_temperature"], "")
                    self.assertEqual(row["flips"], "0")
                    self.assertEqual(row["status"], "right_censored" if experiment == "hcfirst" else "control")
                    if experiment == "hcfirst":
                        self.assertEqual(row["hcfirst"], "")
                        self.assertEqual(row["search_max_total_acts"], "1000000")


if __name__ == "__main__":
    unittest.main()
