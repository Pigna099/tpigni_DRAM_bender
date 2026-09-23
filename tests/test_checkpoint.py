import csv
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np

from characterization.checkpoint import RunStore, provenance, unit_id, work_units
from main import run_experiment
from test_measurement import config, threshold_measure, ber_result, search_result


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / "run"
        self.config = config()
        self.unit = next(work_units(self.config))
        self.results = [ber_result(threshold_measure(None), 0) for _ in range(2)]
        self.provenance = self.enterContext(patch("characterization.checkpoint.provenance",
                                                 return_value={"test_source": 1}))

    def open(self, conf=None):
        return RunStore(self.directory, conf or self.config)

    def rows(self):
        with (self.directory / "summary.csv").open() as f:
            return list(csv.DictReader(f))

    def test_zero_units_are_committed_without_pickle(self):
        with self.open() as store:
            store.commit(self.unit, self.results, 0.1)
            with self.assertRaises(FileExistsError):
                store.commit(self.unit, self.results, 0.1)
        manifest = (self.directory / "manifest.json").read_bytes()
        (self.directory / "summary.csv").write_text("broken CSV")
        with self.open() as store:
            self.assertEqual(store.completed, {unit_id(self.unit)})
        self.assertEqual(manifest, (self.directory / "manifest.json").read_bytes())
        self.assertEqual(len(self.rows()), 2)
        self.assertTrue(all(r["flips"] == "0" and r["ber"] == "0.0" for r in self.rows()))
        for path in [self.directory / "manifest.json", self.directory / "summary.csv",
                     *list((self.directory / "units").glob("*.npz"))]:
            self.assertEqual(path.stat().st_mode & 0o444, 0o444)
        with np.load(next((self.directory / "units").glob("*.npz")), allow_pickle=False) as archive:
            self.assertEqual(archive["result_masks"].shape, (2, 8192))
            self.assertEqual(archive["probe_masks"].dtype, np.uint8)
            self.assertEqual(archive["probe_masks"].sum(), 0)
            self.assertEqual(len(json.loads(archive["metadata"].item())["results"]), 2)

    def test_masks_brackets_and_censored_searches_survive_resume(self):
        c = config("hcfirst")
        results = [search_result(threshold_measure(73), 1, 100),
                   search_result(threshold_measure(None), 1, 100)]
        unit = next(work_units(c))
        with self.open(c) as store:
            store.commit(unit, results, 0.1)
        (self.directory / "summary.csv").unlink()
        with self.open(c) as store:
            self.assertEqual(len(store.completed), 1)
        with np.load(next((self.directory / "units").glob("*.npz")), allow_pickle=False) as archive:
            np.testing.assert_array_equal(archive["result_masks"], np.stack([r.mask for r in results]))
            np.testing.assert_array_equal(archive["probe_masks"], np.stack([p.mask for r in results for p in r.probes]))
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[1]["status"], "right_censored")
        self.assertEqual(rows[1]["hcfirst"], "")
        self.assertEqual(rows[1]["bracket_low"], "100")

    def test_temperature_survives_csv_rebuild_and_is_bound_to_manifest(self):
        # Include a comma to exercise CSV quoting as well as archive persistence.
        temperature = "50 C, external setpoint"
        for experiment in ("ber", "hcfirst"):
            with self.subTest(experiment=experiment):
                directory = Path(self.temp.name) / experiment
                c = config(experiment, "--temperature", temperature)
                unit = next(work_units(c))
                results = self.results if experiment == "ber" else [
                    search_result(threshold_measure(73), 1, 100),
                    search_result(threshold_measure(None), 1, 100)]
                with RunStore(directory, c) as store:
                    store.commit(unit, results, 0.1)
                manifest = json.loads((directory / "manifest.json").read_text())
                self.assertEqual(manifest["Temperature"], temperature)
                path = next((directory / "units").glob("*.npz"))
                before = path.read_bytes()
                with np.load(path, allow_pickle=False) as archive:
                    metadata = json.loads(archive["metadata"].item())
                    self.assertEqual(metadata["Temperature"], temperature)
                    self.assertEqual(metadata["temperature_source"], c.temperature_source)
                    self.assertTrue(all(r["Temperature"] == temperature for r in metadata["results"]))
                    np.testing.assert_array_equal(archive["result_masks"], np.stack([r.mask for r in results]))
                (directory / "summary.csv").write_text("discarded derivative")
                alias_config = config(experiment, "--temperature-label", temperature)
                with RunStore(directory, alias_config):
                    pass
                with (directory / "summary.csv").open() as f:
                    rows = list(csv.DictReader(f))
                self.assertEqual(len(rows), 2)
                self.assertTrue(all(r["Temperature"] == temperature for r in rows))
                self.assertTrue(all(r["temperature_label"] == temperature for r in rows))
                self.assertEqual(before, path.read_bytes())
                with self.assertRaisesRegex(ValueError, "incompatible resume"):
                    with RunStore(directory, replace(c, temperature_label="60 C")):
                        pass

    def test_interrupt_before_archive_publication(self):
        with self.open() as store:
            def partial_write(file, **kwargs):
                file.write(b"partial zip")
                raise KeyboardInterrupt()
            with patch("characterization.checkpoint.np.savez_compressed", side_effect=partial_write):
                with self.assertRaises(KeyboardInterrupt):
                    store.commit(self.unit, self.results, 0.1)
            self.assertEqual(list(store.units_dir.glob("*.npz")), [])
        with self.open() as store:
            self.assertFalse(store.completed)
            store.commit(self.unit, self.results, 0.1)
        self.assertEqual(len(self.rows()), 2)

    def test_interrupt_after_archive_before_csv(self):
        with self.open() as store:
            with patch.object(store, "csv_rows", side_effect=KeyboardInterrupt()):
                with self.assertRaises(KeyboardInterrupt):
                    store.commit(self.unit, self.results, 0.1)
            self.assertEqual(len(list(store.units_dir.glob("*.npz"))), 1)
            self.assertEqual(len(self.rows()), 0)
        with self.open() as store:
            self.assertEqual(len(store.completed), 1)
        self.assertEqual(len(self.rows()), 2)

    def test_exclusive_writer_and_incompatible_manifest(self):
        with self.open():
            with self.assertRaisesRegex(RuntimeError, "another writer"):
                with self.open():
                    pass
        for bad_config, source in ((replace(self.config, repetitions=3), {"test_source": 1}),
                                   (self.config, {"test_source": 2}),
                                   (replace(self.config, temperature_label="50 C"), {"test_source": 1})):
            self.provenance.return_value = source
            with self.assertRaisesRegex(ValueError, "incompatible resume"):
                with self.open(bad_config):
                    pass
        with self.open():
            pass  # incompatible opens released the lock

    def test_corrupt_mask_is_rejected(self):
        with self.open() as store:
            store.commit(self.unit, self.results, 0.1)
        path = next((self.directory / "units").glob("*.npz"))
        with np.load(path, allow_pickle=False) as f:
            arrays = {k: f[k] for k in f.files}
        arrays["probe_masks"][0, 0] = 255
        np.savez_compressed(path, **arrays)
        with self.assertRaisesRegex(ValueError, "mask mismatch"):
            with self.open():
                pass

    def test_runner_reruns_whole_interrupted_unit_and_skips_complete(self):
        from contextlib import nullcontext
        c = replace(self.config, rows=(8, 16))
        calls = []
        interrupted = False
        def make_measure(board, config, victim, **kwargs):
            def measure(hc):
                nonlocal interrupted
                calls.append(victim)
                if victim == 16 and not interrupted and calls.count(16) == 2:
                    interrupted = True
                    raise KeyboardInterrupt()
                return threshold_measure(None).probe(hc)
            return SimpleNamespace(probe=measure)
        with patch("main.hardware_session", side_effect=lambda c: nullcontext(None)), \
             patch("main.Measurement", side_effect=make_measure):
            with self.assertRaises(KeyboardInterrupt):
                run_experiment(c, self.directory)
            run_experiment(c, self.directory)
        self.assertEqual(calls, [8, 8, 16, 16, 16, 16])
        self.assertEqual(len(self.rows()), 4)
        with patch("main.hardware_session", side_effect=AssertionError("must not open hardware")):
            run_experiment(c, self.directory)
        self.assertEqual(len(self.rows()), 4)

    def test_minimal_provenance_preserves_resume_checks_without_git_or_bitstream(self):
        # A source snapshot needs neither a Git checkout nor a local bitfile
        # to save results. The setup record supplies the fixed identifiers.
        root = Path(self.temp.name) / "source"
        (root / "characterization").mkdir(parents=True)
        (root / "local").mkdir()
        program = root / "characterization/programs.py"
        program.write_text("original schedule")
        (root / "main.py").write_text("experiment entry point")
        (root / "pyproject.toml").write_text("package definition")
        setup = {"source_commit": "fixed-api", "bitstream_commit": "fixed-images",
                 "bitstream": "missing-but-recorded.bit", "bitstream_sha256": "recorded-file-hash"}
        setup_path = root / "local/u200_samsung_sa0.json"
        setup_path.write_text(json.dumps(setup))
        self.provenance.side_effect = provenance
        with patch("characterization.checkpoint.ROOT", root):
            with self.open() as store:
                store.commit(self.unit, self.results, 0.1)
            manifest_path = self.directory / "manifest.json"
            before = manifest_path.read_bytes()
            manifest = json.loads(before)
            self.assertEqual(manifest["schema_version"], 1)
            self.assertEqual(manifest["provenance"]["recorded_setup"], setup)
            self.assertEqual(len(manifest["provenance"]["experiment_sha256"]), 64)
            with self.open() as store:
                self.assertEqual(store.completed, {unit_id(self.unit)})
            program.write_text("changed schedule")
            with self.assertRaisesRegex(ValueError, "incompatible resume"):
                with self.open():
                    pass
            program.write_text("original schedule")
            setup_path.write_text(json.dumps({**setup, "bitstream_commit": "different-images"}))
            with self.assertRaisesRegex(ValueError, "incompatible resume"):
                with self.open():
                    pass
            self.assertEqual(manifest_path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
