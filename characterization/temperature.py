"""Fixed-run temperature-control contract and safety monitor."""

from dataclasses import asdict, dataclass
from contextlib import contextmanager
import importlib.util
import json
import logging
from pathlib import Path
import threading
import time

LOG = logging.getLogger("characterization")

LIQUID_ON_TARGET = -50.0
LIQUID_OFF_TARGET = 50.0


@dataclass(frozen=True)
class TemperatureConfig:
    dram_target: float
    liquid_target: float
    poll_seconds: float = 1.0
    safety_margin_dram: float = 5.0
    safety_margin_liquid: float = 15.0
    safety_delay_seconds: float = 300.0
    safe_target: float = 50.0

    def as_dict(self):
        return asdict(self)


def load_config(path):
    """Load and validate an immutable temperature configuration."""
    path = Path(path)
    with path.open() as stream:
        values = json.load(stream)
    required = ("dram_target", "liquid_target")
    missing = [name for name in required if name not in values]
    if missing:
        raise ValueError(f"missing temperature values: {', '.join(missing)}")
    try:
        config = TemperatureConfig(**values)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid temperature configuration: {error}") from error
    validate_config(config)
    return config


def validate_config(config):
    numeric = ("dram_target", "liquid_target", "poll_seconds", "safety_margin_dram",
               "safety_margin_liquid", "safety_delay_seconds", "safe_target")
    for name in numeric:
        value = getattr(config, name)
        if not isinstance(value, (int, float)) or not float(value) == value:
            raise ValueError(f"{name} must be finite")
    if config.liquid_target > config.dram_target:
        raise ValueError("liquid target must not exceed DRAM target")
    if config.poll_seconds <= 0 or config.safety_delay_seconds <= 0:
        raise ValueError("poll_seconds and safety_delay_seconds must be positive")
    if config.safety_margin_dram < 0 or config.safety_margin_liquid < 0:
        raise ValueError("temperature safety margins must not be negative")


class TemperatureMonitor:
    """Poll two controller objects and stop the run on a safety violation.

    Controllers must provide ``changeTargetTemp`` and ``readTempSilent``.
    ``shutdown`` is intentionally injected so hardware-specific cleanup stays
    outside this module and remains straightforward to fake in tests.
    """

    def __init__(self, dram_controller, liquid_controller, config, stop_event=None,
                 shutdown=None, clock=time.monotonic, monitor_only=False):
        validate_config(config)
        self.dram_controller = dram_controller
        self.liquid_controller = liquid_controller
        self.config = config
        self.stop_event = stop_event or threading.Event()
        self.ready_event = threading.Event()
        self.shutdown = shutdown or (lambda: None if monitor_only else self._default_shutdown())
        self.monitor_only = monitor_only
        self.clock = clock
        self.fault = None
        self.last_sample = None
        self._dram_ready = config.dram_target >= 0
        self._dram_hot_since = None
        self._liquid_hot_since = None

    @staticmethod
    def _temperature(controller):
        value = controller.readTempSilent()
        if value > 32767:
            value -= 65536
        return value / 10.0

    def apply_targets(self):
        # For sub-zero targets, the liquid loop must cool the DRAM to 0 C
        # before the DRAM controller is switched to its negative setpoint.
        self.liquid_controller.changeTargetTemp(self.config.liquid_target)
        if self._dram_ready:
            self.dram_controller.changeTargetTemp(self.config.dram_target)

    def activate_dram_target(self, dram_temperature):
        if not self._dram_ready and dram_temperature <= 0:
            self.dram_controller.changeTargetTemp(self.config.dram_target)
            self._dram_ready = True
            LOG.info("DRAM reached %.1f C; activating target %.1f C",
                     dram_temperature, self.config.dram_target)

    def _default_shutdown(self):
        self.dram_controller.changeTargetTemp(self.config.safe_target)
        self.liquid_controller.changeTargetTemp(self.config.safe_target)
        self.liquid_controller.changeTargetTemp(LIQUID_OFF_TARGET)

    def sample(self):
        dram = self._temperature(self.dram_controller)
        liquid = (self._temperature(self.liquid_controller)
                  if self.liquid_controller is not None else None)
        self.last_sample = (self.clock(), dram, liquid)
        return dram, liquid

    def _safety_fault(self, dram, liquid, now):
        if not self._dram_ready:
            self._dram_hot_since = None
        elif dram > self.config.dram_target + self.config.safety_margin_dram:
            self._dram_hot_since = self._dram_hot_since or now
        else:
            self._dram_hot_since = None
        if liquid > self.config.dram_target + self.config.safety_margin_liquid:
            self._liquid_hot_since = self._liquid_hot_since or now
        else:
            self._liquid_hot_since = None
        if self._dram_hot_since is not None and now - self._dram_hot_since >= self.config.safety_delay_seconds:
            return "DRAM temperature exceeded its safety margin"
        if self._liquid_hot_since is not None and now - self._liquid_hot_since >= self.config.safety_delay_seconds:
            return "liquid temperature exceeded its safety margin"
        return None

    def run(self):
        try:
            if not self.monitor_only:
                self.apply_targets()
            while not self.stop_event.wait(0):
                dram, liquid = self.sample()
                if self.monitor_only:
                    self.stop_event.wait(self.config.poll_seconds)
                    continue
                self.activate_dram_target(dram)
                if self._dram_ready and abs(dram - self.config.dram_target) <= 1.0:
                    if not self.ready_event.is_set():
                        LOG.info("DRAM target reached: %.1f C (target %.1f C)",
                                 dram, self.config.dram_target)
                    self.ready_event.set()
                fault = self._safety_fault(dram, liquid, self.last_sample[0])
                LOG.info("temperature DRAM=%.1f C liquid=%.1f C", dram, liquid)
                if fault:
                    self.fault = RuntimeError(fault)
                    self.stop_event.set()
                    self.shutdown()
                    return
                self.stop_event.wait(self.config.poll_seconds)
        except Exception as error:
            self.fault = error
            self.stop_event.set()
            try:
                self.shutdown()
            finally:
                return

    def start(self):
        thread = threading.Thread(target=self.run, name="temperature-monitor", daemon=True)
        thread.start()
        return thread

    def close(self):
        self.stop_event.set()
        self.shutdown()

    def check(self):
        if self.fault is not None:
            raise RuntimeError(f"temperature safety stop: {self.fault}") from self.fault

    def wait_until_ready(self):
        while not self.ready_event.wait(self.config.poll_seconds):
            self.check()
        self.check()


def open_ft200_pair():
    """Open the two legacy FT200 controllers without importing them normally."""
    return open_ft200_controller(0, 2), open_ft200_controller(1, 1)


def open_ft200_controller(device_id, address):
    """Open one legacy FT200 controller without importing it normally."""
    module_path = Path(__file__).resolve().parent / "ft200" / "ft200.py"
    if not module_path.exists():
        raise RuntimeError(f"legacy FT200 module not found: {module_path}")
    spec = importlib.util.spec_from_file_location("dram_rd_ft200", module_path)
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
        return module.FT200(True, _deviceID=device_id, _ft200_addr=address)
    except ImportError as error:
        raise RuntimeError("FT200 control requires minimalmodbus and pyserial") from error


@contextmanager
def temperature_session(config):
    """Run the fixed temperature monitor when the CLI enabled it."""
    if config.temperature_config is None:
        if config.temperature_label == "vanilla":
            dram_controller = open_ft200_controller(0, 2)
            monitor_config = TemperatureConfig(
                dram_target=50.0,
                liquid_target=50.0,
                poll_seconds=config.temperature_poll_seconds,
            )
            monitor = TemperatureMonitor(dram_controller, None, monitor_config,
                                          monitor_only=True)
            thread = monitor.start()
            try:
                yield monitor
            finally:
                monitor.close()
                thread.join(timeout=max(1.0, monitor_config.poll_seconds * 2))
            return
        yield None
        return
    temperature = load_config(config.temperature_config)
    dram_controller, liquid_controller = open_ft200_pair()
    monitor = TemperatureMonitor(dram_controller, liquid_controller, temperature)
    thread = monitor.start()
    try:
        LOG.info("Waiting for DRAM temperature %.1f C +/- 1.0 C before opening FPGA",
                 temperature.dram_target)
        monitor.wait_until_ready()
        yield monitor
    finally:
        monitor.close()
        thread.join(timeout=max(1.0, temperature.poll_seconds * 2))
