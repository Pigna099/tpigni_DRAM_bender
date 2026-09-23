"""One fixed legacy-tAggON / per-aggressor HC point, fresh repeated trials."""

from .measurement import TrialResult


def run_unit(measure, config):
    """Yield each freshly initialized trial; main.py handles progress and storage."""
    for _ in range(config.repetitions):
        probe = measure.probe(config.hc)
        if probe.hc != config.hc:
            raise ValueError("measurement returned the wrong hammer count")
        metadata = probe.metadata()
        metadata.update(status="control" if config.hc == 0 else "measured", selected_probe_index=0)
        yield TrialResult(metadata, probe.mask.copy(), [probe])
