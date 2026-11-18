import json
from pathlib import Path
import tempfile
import threading
import unittest

from characterization.temperature import TemperatureConfig, TemperatureMonitor, load_config


class FakeController:
    def __init__(self, raw=250):
        self.raw = raw
        self.targets = []

    def changeTargetTemp(self, value):
        self.targets.append(value)

    def readTempSilent(self):
        return self.raw


class SequenceController(FakeController):
    def __init__(self, values):
        super().__init__()
        self.values = iter(values)

    def readTempSilent(self):
        try:
            return next(self.values)
        except StopIteration:
            return -200


class TemperatureTests(unittest.TestCase):
    def test_loads_fixed_config_and_rejects_invalid_target_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "temperature.json"
            path.write_text(json.dumps({"dram_target": 50, "liquid_target": -50}))
            config = load_config(path)
            self.assertEqual(config.dram_target, 50)
            self.assertEqual(config.liquid_target, -50)
            path.write_text(json.dumps({"dram_target": 20, "liquid_target": 30}))
            with self.assertRaisesRegex(ValueError, "liquid target"):
                load_config(path)

    def test_decodes_signed_controller_temperature(self):
        controller = FakeController(65526)
        monitor = TemperatureMonitor(controller, FakeController(), TemperatureConfig(50, -50))
        self.assertEqual(monitor._temperature(controller), -1.0)

    def test_subzero_target_starts_liquid_then_activates_dram_at_zero(self):
        dram = FakeController(50)
        liquid = FakeController()
        config = TemperatureConfig(-20, -50)
        monitor = TemperatureMonitor(dram, liquid, config)

        monitor.apply_targets()
        self.assertEqual(dram.targets, [])
        self.assertEqual(liquid.targets, [-50])

        monitor.activate_dram_target(0.1)
        self.assertEqual(dram.targets, [])
        monitor.activate_dram_target(0)
        self.assertEqual(dram.targets, [-20])
        monitor.activate_dram_target(-1)
        self.assertEqual(dram.targets, [-20])

    def test_ready_wait_requires_target_within_one_degree(self):
        dram = SequenceController([100, 0, -200])
        liquid = FakeController()
        config = TemperatureConfig(-20, -50, poll_seconds=0.001)
        monitor = TemperatureMonitor(dram, liquid, config)
        thread = monitor.start()
        try:
            monitor.wait_until_ready()
            self.assertTrue(monitor.ready_event.is_set())
            self.assertEqual(dram.targets, [-20])
        finally:
            monitor.close()
            thread.join(timeout=1)

    def test_safety_fault_shutdowns_after_delay(self):
        dram = FakeController()
        liquid = FakeController()
        stopped = threading.Event()
        config = TemperatureConfig(50, -50, safety_delay_seconds=5)
        monitor = TemperatureMonitor(dram, liquid, config, stopped)
        monitor._safety_fault(60, 0, 10)
        self.assertIsNone(monitor._safety_fault(60, 0, 14))
        self.assertEqual(monitor._safety_fault(60, 0, 15), "DRAM temperature exceeded its safety margin")
        monitor.close()
        self.assertEqual(dram.targets, [50.0])
        self.assertEqual(liquid.targets, [50.0, 50.0])


if __name__ == "__main__":
    unittest.main()
