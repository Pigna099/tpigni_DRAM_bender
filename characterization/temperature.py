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
    # The FPGA is opened only after the DRAM has stayed within +/-1 C of the
    # target for this long without leaving the band (0 = open immediately).
    settle_seconds: float = 30.0
    # Pulsed-pump regime, used when the DRAM target is not below the liquid
    # temperature. The pump is cycled by the DRAM temperature while the
    # Peltier (cooling only) keeps trimming to dram_target.
    pump_min_seconds: float = 10.0
    pump_band_high: float = 1.0
    pump_band_low: float = 1.0
    pulse_margin: float = 2.0
    # "pi": PI controller with a time-proportional pump (default).
    # "bangbang": switch on pump_band_high / pump_band_low.
    pump_mode: str = "pi"
    # PI mode. The PI output is a duty in [0, 1] applied over one window:
    # the pump is on for duty * window seconds at the start of each window.
    # pump_kp is duty per C of error, pump_ki is duty per C per second.
    pump_window_seconds: float = 30.0
    pump_kp: float = 0.1
    pump_ki: float = 0.0005
    pump_duty_initial: float = 0.3
    # Longest continuous pump-on time while near the target.
    pump_max_on_seconds: float = 15.0
    # Further than this above the target (e.g. during pull-down) the pump stays
    # on continuously and the on-time limit does not apply.
    pump_full_on_above: float = 3.0

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
               "safety_margin_liquid", "safety_delay_seconds", "safe_target",
               "settle_seconds",
               "pump_min_seconds", "pump_band_high", "pump_band_low", "pulse_margin",
               "pump_window_seconds", "pump_kp", "pump_ki", "pump_duty_initial",
               "pump_max_on_seconds", "pump_full_on_above")
    for name in numeric:
        value = getattr(config, name)
        if not isinstance(value, (int, float)) or not float(value) == value:
            raise ValueError(f"{name} must be finite")
    if config.liquid_target > config.dram_target:
        raise ValueError("liquid target must not exceed DRAM target")
    if config.settle_seconds < 0:
        raise ValueError("settle_seconds must not be negative")
    if config.poll_seconds <= 0 or config.safety_delay_seconds <= 0:
        raise ValueError("poll_seconds and safety_delay_seconds must be positive")
    if config.safety_margin_dram < 0 or config.safety_margin_liquid < 0:
        raise ValueError("temperature safety margins must not be negative")
    if config.pump_min_seconds < 0 or config.pulse_margin < 0:
        raise ValueError("pump_min_seconds and pulse_margin must not be negative")
    if config.pump_band_high <= 0 or config.pump_band_low <= 0:
        raise ValueError("pump temperature bands must be positive")
    if config.pump_mode not in ("pi", "bangbang"):
        raise ValueError("pump_mode must be 'pi' or 'bangbang'")
    if config.pump_kp < 0 or config.pump_ki < 0:
        raise ValueError("pump_kp and pump_ki must not be negative")
    if not 0 <= config.pump_duty_initial <= 1:
        raise ValueError("pump_duty_initial must be between 0 and 1")
    if config.pump_window_seconds <= 0 or config.pump_max_on_seconds <= 0 \
            or config.pump_full_on_above <= 0:
        raise ValueError("pump_window_seconds, pump_max_on_seconds and "
                         "pump_full_on_above must be positive")
    if config.pump_mode == "pi":
        if config.pump_window_seconds < 2 * config.pump_min_seconds:
            raise ValueError("pump_window_seconds must be at least twice pump_min_seconds")
        if config.pump_max_on_seconds < config.pump_min_seconds:
            raise ValueError("pump_max_on_seconds must not be below pump_min_seconds")


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
        self._in_band_since = None
        self._dram_hot_since = None
        self._liquid_hot_since = None
        self.pulsed = False
        self.pump_on = True
        self._pump_changed_at = None
        self._reset_pi()

    def _reset_pi(self):
        self._integral = None
        self._pi_last = None
        self._window_start = None
        self._on_time = 0.0
        self._credit = 0.0

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
        self.pump_on = True
        self._pump_changed_at = self.clock()
        if self._dram_ready:
            self.dram_controller.changeTargetTemp(self.config.dram_target)

    def _set_pump(self, on, now, reason):
        """Switch the pump through the liquid controller setpoint.

        The freezer stays on; a setpoint above the liquid temperature only
        stops circulation, and the liquid target restarts it.
        """
        target = self.config.liquid_target if on else LIQUID_OFF_TARGET
        self.liquid_controller.changeTargetTemp(target)
        self.pump_on = on
        self._pump_changed_at = now
        LOG.info("pump %s (%s)", "ON" if on else "OFF", reason)

    def _update_regime(self, liquid):
        """Select the pulsed-pump regime while the DRAM target is not below liquid.

        Enter when dram_target > liquid - pulse_margin; leave only when
        dram_target < liquid - 2 * pulse_margin, so noise cannot chatter it.
        """
        margin = self.config.pulse_margin
        if not self.pulsed and self.config.dram_target > liquid - margin:
            self.pulsed = True
            LOG.info("DRAM target %.1f C is not below liquid %.1f C; pulsing the pump",
                     self.config.dram_target, liquid)
        elif self.pulsed and self.config.dram_target < liquid - 2 * margin:
            self.pulsed = False
            LOG.info("DRAM target %.1f C is below liquid %.1f C; continuous pump",
                     self.config.dram_target, liquid)

    def control_pump(self, dram, liquid, now):
        """Cycle the pump around the DRAM target when the liquid is colder than it.

        The Peltier can only cool, so the pump is switched off when the DRAM
        undershoots the target and back on when it overshoots. Each state
        lasts at least ``pump_min_seconds``.
        """
        if self.monitor_only or self.liquid_controller is None or liquid is None:
            return
        self._update_regime(liquid)
        if not self.pulsed:
            self._reset_pi()
            if not self.pump_on:
                self._set_pump(True, now, "continuous regime")
            return
        if not self._dram_ready:
            return  # pull the DRAM down with the pump on until the Peltier gate opens
        if self._pump_changed_at is None:
            self._pump_changed_at = now
        if self.config.pump_mode == "pi":
            self._control_pump_pi(dram, now)
            return
        if now - self._pump_changed_at < self.config.pump_min_seconds:
            return
        target = self.config.dram_target
        if self.pump_on and dram < target - self.config.pump_band_low:
            self._set_pump(False, now, f"DRAM {dram:.1f} C below target {target:.1f} C")
        elif not self.pump_on and dram > target + self.config.pump_band_high:
            self._set_pump(True, now, f"DRAM {dram:.1f} C above target {target:.1f} C")

    def _apply_pump(self, want_on, now, reason):
        """Move the pump to the wanted state once its minimum dwell has passed."""
        if want_on == self.pump_on:
            return
        if now - self._pump_changed_at < self.config.pump_min_seconds:
            return
        self._set_pump(want_on, now, reason)

    def _control_pump_pi(self, dram, now):
        """PI control of the pump duty, applied as an on-time inside each window.

        error > 0 means the DRAM is too warm and needs more cooling. The Peltier
        only cools, so the pump on-time is what lifts the DRAM back up toward
        the target by being shortened. On-times shorter than ``pump_min_seconds``
        are accumulated and released as one pulse, so the average duty holds.
        """
        cfg = self.config
        window = cfg.pump_window_seconds
        error = dram - cfg.dram_target
        cap = min(cfg.pump_max_on_seconds, window - cfg.pump_min_seconds)
        duty_max = cap / window

        if self._integral is None:
            self._integral = min(cfg.pump_duty_initial, duty_max)
            self._pi_last = now
        dt = min(now - self._pi_last, 5 * cfg.poll_seconds)
        self._pi_last = now
        # Conditional integration: no windup while the duty is saturated.
        raw = cfg.pump_kp * error + self._integral
        if not ((raw >= duty_max and error > 0) or (raw <= 0 and error < 0)):
            self._integral = min(max(self._integral + cfg.pump_ki * error * dt, 0.0), duty_max)
        duty = min(max(cfg.pump_kp * error + self._integral, 0.0), duty_max)

        if error > cfg.pump_full_on_above:
            # Far above the target there is no risk of undershoot: cool without a limit.
            self._window_start = None
            self._credit = 0.0
            self._apply_pump(True, now, f"DRAM {dram:.1f} C far above target")
            return

        if self._window_start is None or now - self._window_start >= window:
            desired = duty * window + self._credit
            if desired < cfg.pump_min_seconds:
                self._on_time = 0.0
                self._credit = desired if duty > 0 else 0.0
            else:
                self._on_time = min(desired, cap)
                self._credit = 0.0
            self._window_start = now
            LOG.info("pump window: DRAM=%.1f C error=%+.1f C duty=%.2f on-time=%.0f s",
                     dram, error, duty, self._on_time)

        want_on = now - self._window_start < self._on_time
        if want_on and error < -cfg.pump_band_low:
            want_on = False  # undershoot cut-off: the Peltier cannot heat the DRAM back
        self._apply_pump(want_on, now, f"PI window, DRAM {dram:.1f} C")

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

    def _update_ready(self, dram, now):
        """Open the ready gate once the DRAM stayed within 1 C of the target for settle_seconds."""
        if self.ready_event.is_set():
            return
        if self._dram_ready and abs(dram - self.config.dram_target) <= 1.0:
            if self._in_band_since is None:
                self._in_band_since = now
                LOG.info("DRAM target reached: %.1f C (target %.1f C); settling for %.0f s",
                         dram, self.config.dram_target, self.config.settle_seconds)
            if now - self._in_band_since >= self.config.settle_seconds:
                LOG.info("DRAM stable at %.1f C; opening FPGA", dram)
                self.ready_event.set()
        elif self._in_band_since is not None:
            LOG.info("DRAM left the target band (%.1f C); settling restarts", dram)
            self._in_band_since = None

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
                self.control_pump(dram, liquid, self.last_sample[0])
                self._update_ready(dram, self.last_sample[0])
                fault = self._safety_fault(dram, liquid, self.last_sample[0])
                LOG.info("temperature DRAM=%.1f C liquid=%.1f C pump=%s",
                         dram, liquid, "on" if self.pump_on else "off")
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
