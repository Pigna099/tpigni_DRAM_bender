"""Human-reviewable, hardware-free command traces and workload estimates."""

from collections import Counter
import math

from drambender.api import DDR4Target

from .programs import hammer_program, initialize_aggressors, initialize_victim, read_victim
from .timing import hammer_timing


def describe_program(program, trace_limit=20):
    trace = program.trace_dram_commands(max_instructions=10000)
    if trace.truncated:
        raise RuntimeError("audit trace was truncated")
    events = [{"command": e.command, "time_ns": e.time_ns, "slot": e.slot,
               "bank": e.bank, "row": e.row, "column": e.column, "rank": e.rank}
              for e in trace.events]
    listing = str(program).splitlines()
    return {"instructions": program.instruction_count, "modeled_ns": trace.total_ns,
            "command_counts": dict(Counter(e.command for e in trace.events)),
            "act_rows": [e.row for e in trace.events if e.command == "ACT"],
            "decoded_instructions": listing if len(listing) <= 40 else listing[:20] + ["..."] + listing[-12:],
            "trace_events": events if len(events) <= trace_limit else events[:trace_limit//2] + events[-trace_limit//2:],
            "trace_events_omitted": max(0, len(events) - trace_limit)}


def build_audit(config):
    target = DDR4Target(rank=config.rank)
    addr = config.addresses(config.rows[0])
    first_pattern = config.patterns[0]
    static = {
        "aggressor_initialization": initialize_aggressors(target, config.bank, addr["upper"], addr["lower"],
                                                         config.aggressor_byte(first_pattern) * 0x01010101),
        "victim_initialization": initialize_victim(target, config.bank, addr["victim"], first_pattern * 0x01010101,
                                                  config.victim_write_recovery_cycles),
        "victim_readback": read_victim(target, config.bank, addr["victim"]),
    }
    points, min_probes, max_probes = [], 0, 0
    for steps in config.taggon_steps:
        for mode in config.modes:
            timing = hammer_timing(steps, mode, config.rank)
            maximum = timing.max_hc(config.duration_limit_ms, config.count_ceiling,
                                    total_act_ceiling=config.total_act_ceiling)
            if config.experiment == "hcfirst":
                per_trial_min = 1 if maximum == config.min_hc else 2
                per_trial_max = per_trial_min + max(0, math.ceil(math.log2(max(1, (maximum - config.min_hc) / config.resolution))))
                hc = maximum
            else:
                per_trial_min = per_trial_max = len(config.hc_values)
                hc = config.hc if config.hc is not None else config.hc_values[-1]
            units = len(config.rows) * len(config.patterns) * config.repetitions
            min_probes += per_trial_min * units
            max_probes += per_trial_max * units
            points.append({"legacy_taggon_steps": steps, "mode": mode, **timing.as_dict(),
                           "duration_max_hc": timing.max_hc(config.duration_limit_ms),
                           "search_max_hc": maximum if config.experiment == "hcfirst" else None,
                           "search_max_total_acts": maximum * timing.acts_per_hc if config.experiment == "hcfirst" else None,
                           "selected_hc_bound_or_point": hc,
                           "selected_total_hammer_acts": hc * timing.acts_per_hc,
                           "selected_hammer_ms": timing.duration_ns(hc) / 1e6,
                           "representative_hc2_program": describe_program(hammer_program(
                               target, config.bank, addr["upper"], addr["lower"], mode, 2, steps))})
    return {"hardware_opened": False, "configuration": config.as_dict(),
            "workload": {"units": config.unit_count, "independent_results": config.unit_count * config.repetitions,
                         "minimum_probes": min_probes, "maximum_probes": max_probes,
                         "tested_bits_per_probe": 65536},
            "address_mapping": [{"physical_victim": r, **config.addresses(r)} for r in config.rows],
            "pattern_words": [{"victim": f"{p * 0x01010101:08x}",
                               "aggressor": f"{config.aggressor_byte(p) * 0x01010101:08x}"} for p in config.patterns],
            "instruction_capacity": target.instruction_capacity,
            "static_programs": {name: describe_program(p) for name, p in static.items()},
            "hammer_points": points,
            "timing_scope": "V2 VM model of hammer only; excludes transport, host gaps, initialization and readback",
            "Temperature": config.temperature_label, "temperature_source": config.temperature_source}
