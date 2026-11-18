"""Run HCFirst or BER: parse, audit, resume, measure, commit, restore hardware."""

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import logging
import signal
import time

from characterization import ber_runner, hcfirst_runner
from characterization.audit import build_audit
from characterization.checkpoint import RunStore, unit_id, work_units
from characterization.config import build_parser, resolve
from characterization.measurement import Measurement, hardware_session
from characterization.temperature import temperature_session

LOG = logging.getLogger("characterization")


@contextmanager
def handled_signals():
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    previous = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


@contextmanager
def run_logging(directory, verbose):
    formatter = logging.Formatter("%(asctime)sZ %(levelname)s %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    formatter.converter = time.gmtime
    handlers = [logging.StreamHandler(), logging.FileHandler(directory / "events.log")]
    old_level = LOG.level
    LOG.setLevel(logging.DEBUG if verbose else logging.INFO)
    LOG.propagate = False
    for handler in handlers:
        handler.setFormatter(formatter)
        LOG.addHandler(handler)
    try:
        yield
    finally:
        for handler in handlers:
            LOG.removeHandler(handler)
            handler.close()
        LOG.setLevel(old_level)


class Progress:
    def __init__(self, total, completed, interval):
        self.total, self.completed, self.initial, self.interval = total, completed, completed, interval
        self.started = self.last = time.monotonic()

    def report(self, key, repetition, repetitions, force=False):
        now = time.monotonic()
        if not force and now - self.last < self.interval:
            return
        fraction = repetition / repetitions
        processed = self.completed - self.initial + fraction
        remaining = self.total - self.completed - fraction
        eta = (now - self.started) / processed * remaining if processed else float("nan")
        LOG.info("progress %d/%d complete units; %s repetition %d/%d; elapsed %.1f s; estimated remaining %.1f s",
                 self.completed, self.total, key, repetition, repetitions, now - self.started, eta)
        self.last = now


def run_experiment(config, directory, *, verbose=False, progress_seconds=10):
    with handled_signals(), RunStore(directory, config) as store:
        with run_logging(store.directory, verbose):
            LOG.info("%s module=%s Temperature=%s (externally controlled; not measured by this code)",
                     config.experiment, config.module, config.temperature_label)
            LOG.info("configuration %s", json.dumps(config.as_dict(), sort_keys=True))
            LOG.info("workload=%d units x %d repetitions; committed=%d; masks are authoritative NPZ archives",
                     config.unit_count, config.repetitions, len(store.completed))
            if len(store.completed) == config.unit_count:
                LOG.info("All units already committed; CSV rebuilt; no hardware opened")
                return
            progress = Progress(config.unit_count, len(store.completed), progress_seconds)
            try:
                with temperature_session(config) as temperature, hardware_session(config) as board:
                    for unit in work_units(config):
                        if temperature is not None:
                            temperature.check()
                        key = unit_id(unit)
                        if key in store.completed:
                            continue
                        LOG.info("starting unit %s mapped_rows=%s", key, config.addresses(unit["victim"]))
                        started = time.monotonic()
                        measure = Measurement(
                            board, config, unit["victim"],
                            **{name: unit[name] for name in ("pattern", "steps", "mode")})
                        if config.experiment == "hcfirst":
                            trials = hcfirst_runner.run_unit(measure, config)
                        else:
                            trials = ber_runner.run_unit(measure, config, unit.get("hc"))
                        results = []
                        for repetition, result in enumerate(trials, start=1):
                            if temperature is not None:
                                temperature.check()
                                _, dram_temperature, liquid_temperature = temperature.last_sample
                                result.metadata["dram_temperature"] = dram_temperature
                                result.metadata["liquid_temperature"] = liquid_temperature
                            results.append(result)
                            progress.report(key, repetition, config.repetitions)
                        store.commit(unit, results, time.monotonic() - started)
                        LOG.info("committed unit %s results=%d flips=%d right_censored=%d",
                                 key, len(results), sum(r.metadata["flips"] for r in results),
                                 sum(r.metadata["status"] == "right_censored" for r in results))
                        progress.completed += 1
                        progress.report(key, 0, config.repetitions, force=progress.completed == progress.total)
            except KeyboardInterrupt:
                LOG.warning("Interrupted; completed archives retained; the unfinished unit will rerun on resume")
                raise
            except Exception:
                LOG.exception("Run failed; incomplete unit not committed; transport errors are not DRAM flips")
                raise
            LOG.info("Run complete at %s", datetime.now(timezone.utc).isoformat())


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        config = resolve(args)
        if args.plan or args.audit_file:
            audit = build_audit(config)
        if args.audit_file:
            # Explicit audit output is an artifact, not a run/checkpoint directory.
            args.audit_file.parent.mkdir(parents=True, exist_ok=True)
            with args.audit_file.open("x") as f:
                json.dump(audit, f, indent=2, allow_nan=False)
                f.write("\n")
        if args.plan:
            print(json.dumps(audit, indent=2, allow_nan=False))
        else:
            run_experiment(config, args.output_dir, verbose=args.verbose,
                           progress_seconds=args.progress_seconds)
        return 0
    except KeyboardInterrupt:
        return 130
    except (ValueError, RuntimeError, OSError) as exc:
        parser.exit(2, f"{parser.prog}: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
