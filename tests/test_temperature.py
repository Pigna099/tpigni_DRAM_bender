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

    def _pulsed_monitor(self, target=-10, liquid_target=-50):
        dram = FakeController()
        liquid = FakeController()
        config = TemperatureConfig(target, liquid_target)
        monitor = TemperatureMonitor(dram, liquid, config, clock=lambda: 0.0)
        monitor.apply_targets()
        monitor.activate_dram_target(0)  # open the sub-zero Peltier gate
        return monitor, dram, liquid

    def test_pulsed_regime_starts_only_when_target_is_not_below_liquid(self):
        monitor, _, liquid = self._pulsed_monitor(target=-30)
        monitor.control_pump(-30, -25, 100)          # target well below liquid
        self.assertFalse(monitor.pulsed)
        monitor.control_pump(-30, -31, 100)          # liquid colder than target
        self.assertTrue(monitor.pulsed)
        monitor.control_pump(-30, -25, 100)          # back below liquid with hysteresis
        self.assertFalse(monitor.pulsed)
        self.assertEqual(liquid.targets, [-50])      # pump never switched

    def test_pump_cycles_on_dram_bands_with_minimum_dwell(self):
        monitor, _, liquid = self._pulsed_monitor()
        liquid_temp = -25
        monitor.control_pump(-11.5, liquid_temp, 10)  # undershoot but only 10 s on
        self.assertTrue(monitor.pump_on)
        monitor.control_pump(-11.5, liquid_temp, 30)  # dwell satisfied -> off
        self.assertFalse(monitor.pump_on)
        monitor.control_pump(-8.0, liquid_temp, 45)   # overshoot but only 15 s off
        self.assertFalse(monitor.pump_on)
        monitor.control_pump(-10.5, liquid_temp, 60)  # inside the band: stay off
        self.assertFalse(monitor.pump_on)
        monitor.control_pump(-8.0, liquid_temp, 60)   # overshoot, 30 s off -> on
        self.assertTrue(monitor.pump_on)
        self.assertEqual(liquid.targets, [-50, 50.0, -50])

    def test_pump_stays_on_until_peltier_gate_opens(self):
        dram = FakeController()
        liquid = FakeController()
        monitor = TemperatureMonitor(dram, liquid, TemperatureConfig(-10, -50), clock=lambda: 0.0)
        monitor.apply_targets()
        monitor.control_pump(-20, -25, 100)           # DRAM already cold, gate closed
        self.assertTrue(monitor.pulsed)
        self.assertTrue(monitor.pump_on)
        self.assertEqual(liquid.targets, [-50])

    def test_leaving_pulsed_regime_restores_pump_immediately(self):
        monitor, _, liquid = self._pulsed_monitor()
        monitor.control_pump(-11.5, -25, 30)
        self.assertFalse(monitor.pump_on)
        monitor.control_pump(-11.5, 5, 31)            # liquid warmed far above target
        self.assertFalse(monitor.pulsed)
        self.assertTrue(monitor.pump_on)

    def test_monitor_only_never_touches_pump(self):
        dram = FakeController()
        monitor = TemperatureMonitor(dram, None, TemperatureConfig(-10, -50), monitor_only=True)
        monitor.control_pump(-20, None, 100)
        self.assertTrue(monitor.pump_on)

    def test_rejects_invalid_pump_settings(self):
        with self.assertRaisesRegex(ValueError, "bands"):
            TemperatureMonitor(FakeController(), FakeController(),
                               TemperatureConfig(-10, -50, pump_band_high=0))
        with self.assertRaisesRegex(ValueError, "pump_min_seconds"):
            TemperatureMonitor(FakeController(), FakeController(),
                               TemperatureConfig(-10, -50, pump_min_seconds=-1))

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
