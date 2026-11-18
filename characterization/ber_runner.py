"""One fixed legacy-tAggON / per-aggressor HC point, fresh repeated trials."""

from .measurement import TrialResult


def run_unit(measure, config, hc=None):
    """Yield each freshly initialized trial; main.py handles progress and storage."""
    hc = config.hc if hc is None else hc
    for _ in range(config.repetitions):
        probe = measure.probe(hc)
        if probe.hc != hc:
            raise ValueError("measurement returned the wrong hammer count")
        metadata = probe.metadata()
        metadata.update(status="control" if hc == 0 else "measured", selected_probe_index=0)
        yield TrialResult(metadata, probe.mask.copy(), [probe])
