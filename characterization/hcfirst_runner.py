"""HCFirst legacy-tAggON sweep runner."""

import logging
import time

from .measurement import TrialResult

LOG = logging.getLogger("characterization")


def run_unit(measure, config):
    """Yield each independent search; main.py handles progress and storage."""
    maximum = measure.timing.max_hc(config.duration_limit_ms, config.count_ceiling,
                                   total_act_ceiling=config.total_act_ceiling)
    minimum, resolution = config.min_hc, config.resolution
    if not 1 <= minimum <= maximum or resolution < 1:
        raise ValueError("invalid HCFirst search interval or resolution")
    max_acts = maximum * measure.timing.acts_per_hc
    LOG.info("HCFirst search bounds %d..%d per aggressor (%d total ACTs at upper bound); "
             "resolution=%d; max modeled hammer=%.6f ms",
             config.min_hc, maximum, max_acts, config.resolution, measure.timing.duration_ns(maximum) / 1e6)
    for repetition in range(config.repetitions):
        started = time.monotonic()
        probes = []

        def probe(hc):
            result = measure.probe(hc)  # executes fresh neighbor/victim initialization
            if result.hc != hc:
                raise ValueError("measurement returned the wrong hammer count")
            probes.append(result)
            return result

        # Test both bounds independently for every repetition. No observed
        # flip at the upper bound is a censored search, not HCFirst=maximum.
        first = probe(minimum)
        last = probe(maximum) if maximum != minimum else first
        nonmonotonic = bool(first.flips and not last.flips)
        if first.flips:
            selected = first
            status, low, high, hcfirst = "left_censored", None, minimum, minimum
        elif not last.flips:
            selected = last
            status, low, high, hcfirst = "right_censored", maximum, None, None
        else:
            status, low, high = "bracketed", minimum, maximum
            selected = last
            while high - low > resolution:
                mid = (low + high) // 2
                candidate = probe(mid)
                if candidate.flips:
                    high, selected = mid, candidate
                else:
                    low = mid
            hcfirst = high

        metadata = selected.metadata()
        metadata.update(status=status, hcfirst=hcfirst, bracket_low=low, bracket_high=high,
                        search_min=minimum, search_max=maximum, search_max_total_acts=max_acts,
                        resolution=resolution, nonmonotonic_bounds=nonmonotonic,
                        selected_probe_index=next(i for i, p in enumerate(probes) if p is selected),
                        host_wall_seconds=time.monotonic() - started,
                        total_probe_hammer_ns=sum(p.hammer_model_ns for p in probes))
        result = TrialResult(metadata, selected.mask.copy(), probes)
        LOG.debug("repetition=%d status=%s HCFirst=%s bracket=[%s,%s] probes=%d", repetition,
                  result.metadata["status"], result.metadata["hcfirst"], result.metadata["bracket_low"],
                  result.metadata["bracket_high"], len(result.probes))
        yield result
