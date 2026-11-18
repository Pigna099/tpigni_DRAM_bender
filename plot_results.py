#!/usr/bin/env python3
"""Plot BER or HCFirst characterization CSV results."""

import argparse
import csv
from pathlib import Path


def find_csv(path):
    path = Path(path)
    if path.is_dir():
        path = path / "summary.csv"
    if not path.is_file():
        raise ValueError(f"CSV file not found: {path}")
    return path


def load_rows(paths):
    rows = []
    for path in paths:
        csv_path = find_csv(path)
        with csv_path.open(newline="") as stream:
            reader = csv.DictReader(stream)
            required = {"repetition", "flips", "ber", "Temperature",
                        "pattern", "aggressor_pattern"}
            missing = required - set(reader.fieldnames or ())
            if missing:
                raise ValueError(f"{csv_path} is missing columns: {', '.join(sorted(missing))}")
            for row in reader:
                row["source"] = csv_path.parent.name
                row["repetition"] = int(row["repetition"])
                row["flips"] = int(row["flips"])
                row["ber"] = float(row["ber"])
                row["dram_temperature"] = (float(row["dram_temperature"])
                                             if row.get("dram_temperature", "") else None)
                row["experiment"] = row.get("experiment", "ber")
                if row["experiment"] == "hcfirst":
                    row["hcfirst_value"] = (float(row["hcfirst"])
                                             if row.get("hcfirst", "") else None)
                    row["search_max_value"] = (float(row["search_max"])
                                                if row.get("search_max", "") else None)
                rows.append(row)
    if not rows:
        raise ValueError(
            "no committed result rows found; the experiment may have stopped "
            "before completing its first work unit"
        )
    return rows


def add_temperature_axis(axis, groups):
    for group in groups.values():
        measured = [row for row in group if row["dram_temperature"] is not None]
        repetitions = [row["repetition"] for row in measured]
        temperatures = [row["dram_temperature"] for row in measured]
        axis.plot(repetitions, temperatures, marker=".", linewidth=1)
    axis.set_ylabel("DRAM temperature (C)")
    axis.grid(True, alpha=0.3)


def plot_title(rows):
    dates = sorted({row["source"][:8] for row in rows})
    formatted_dates = [f"{date[:4]}-{date[4:6]}-{date[6:]}" for date in dates]
    experiments = sorted({row["experiment"] for row in rows})
    experiment_names = {"ber": "BER", "hcfirst": "HCFirst"}
    experiment = ", ".join(experiment_names.get(name, name) for name in experiments)
    targets = sorted({row["Temperature"] for row in rows})
    target = ", ".join(targets)
    patterns = sorted({(row["pattern"], row["aggressor_pattern"]) for row in rows})
    pattern_text = ", ".join(pattern_label(victim, aggressor)
                             for victim, aggressor in patterns)
    return (
        f"{', '.join(formatted_dates)} | {experiment} characterization | "
        f"target DRAM: {target} | pattern: {pattern_text}"
    )


def pattern_label(victim, aggressor):
    victim_bit = int(victim, 16) == 0xFF
    aggressor_bit = int(aggressor, 16) == 0xFF
    return f"A{int(aggressor_bit)}V{int(victim_bit)}"


def condition_label(group, include_steps=False):
    row = group[0]
    fields = [
        pattern_label(row["pattern"], row["aggressor_pattern"]),
        f"victim row={row.get('victim', '')}",
        f"mode={row.get('mode', '')}",
    ]
    if include_steps:
        fields.append(f"tAggON={row.get('steps', '')}")
    return ", ".join(field for field in fields if not field.endswith("="))


def distribution_groups(rows):
    groups = {}
    for row in rows:
        key = (row["source"], row["Temperature"], row.get("mode", ""),
               row.get("victim", ""), row.get("steps", ""),
               row["pattern"], row["aggressor_pattern"])
        groups.setdefault(key, []).append(row)
    return groups


def plot_distribution(rows, pyplot):
    groups = distribution_groups(rows)
    is_hcfirst = rows[0]["experiment"] == "hcfirst"
    metric_key = "hcfirst_value" if is_hcfirst else "ber"
    metric_label = "HCFirst (HC/aggressor)" if is_hcfirst else "BER"
    figure, axes = pyplot.subplots(3, 1, figsize=(11, 12), sharex=True,
                                   constrained_layout=True)
    figure.suptitle(f"{plot_title(rows)} | distribution by victim row")
    for key, group in sorted(groups.items()):
        label = condition_label(group, include_steps="hcfirst" in {row["experiment"] for row in group})
        victim_rows = [int(row["victim"]) for row in group]
        axes[0].scatter(victim_rows, [row["flips"] for row in group], s=14, label=label)
        metric_rows = [(int(row["victim"]), row[metric_key])
                   for row in group if row[metric_key] is not None]
        axes[1].scatter([row[0] for row in metric_rows],
                [row[1] for row in metric_rows], s=14, label=label)
        measured = [row for row in group if row["dram_temperature"] is not None]
        axes[2].scatter([int(row["victim"]) for row in measured],
                        [row["dram_temperature"] for row in measured], s=14, label=label)

    axes[0].set_xlabel("Victim row")
    axes[0].set_ylabel("Flipped bits")
    axes[1].set_xlabel("Victim row")
    axes[1].set_ylabel(metric_label)
    if is_hcfirst:
        axes[1].set_yscale("log")
    else:
        axes[1].set_yscale("symlog", linthresh=1e-8)
    axes[2].set_xlabel("Victim row")
    axes[2].set_ylabel("DRAM temperature (C)")
    for axis in axes:
        axis.grid(True, alpha=0.3)
    if len(groups) <= 12:
        axes[0].legend(fontsize="small")
    return figure


def plot_ber(rows, output, pyplot):
    groups = {}
    for row in rows:
        key = (row["source"], row["Temperature"], row.get("mode", ""),
               row.get("victim", ""), row["pattern"], row["aggressor_pattern"])
        groups.setdefault(key, []).append(row)

    figure, axes = pyplot.subplots(3, 1, figsize=(11, 10), sharex=True, constrained_layout=True)
    figure.suptitle(plot_title(rows))
    for key, group in sorted(groups.items()):
        group.sort(key=lambda row: row["repetition"])
        label = condition_label(group)
        repetitions = [row["repetition"] for row in group]
        axes[0].plot(repetitions, [row["flips"] for row in group], marker=".", linestyle='None', label=label)
        axes[1].plot(repetitions, [row["ber"] for row in group], marker=".", linestyle='None', label=label)

    axes[0].set_ylabel("Flipped bits")
    axes[1].set_ylabel("BER")
    axes[1].set_xlabel("Repetition")
    axes[1].set_yscale("log")
    axes[0].grid(True, alpha=0.3)
    axes[1].grid(True, alpha=0.3)
    add_temperature_axis(axes[2], groups)
    if len(groups) <= 12:
        axes[0].legend(fontsize="small")
    return figure


def plot_ber_sweep(rows, output, pyplot):
    import numpy as np

    groups = {}
    for row in rows:
        key = (row["source"], row["Temperature"], row.get("mode", ""),
               row["pattern"], row["aggressor_pattern"])
        groups.setdefault(key, {}).setdefault(int(row["hc_per_aggressor"]), []).append(row)

    figure, axes = pyplot.subplots(3, 1, figsize=(11, 10), sharex=True,
                                   constrained_layout=True)
    figure.suptitle(f"{plot_title(rows)} | BER versus HC sweep")
    for key, points in sorted(groups.items()):
        hcs = sorted(points)
        flips = [np.asarray([row["flips"] for row in points[hc]]) for hc in hcs]
        bers = [np.asarray([row["ber"] for row in points[hc]]) for hc in hcs]
        temperatures = [
            np.asarray([row["dram_temperature"] for row in points[hc]
                        if row["dram_temperature"] is not None])
            for hc in hcs
        ]
        label = " / ".join(value for value in key if value != "")
        for axis, values, ylabel in (
            (axes[0], flips, "Flipped bits"),
            (axes[1], bers, "BER"),
        ):
            medians = [np.median(value) for value in values]
            lower = [np.percentile(value, 10) for value in values]
            upper = [np.percentile(value, 90) for value in values]
            axis.plot(hcs, medians, marker="o", label=label)
            axis.fill_between(hcs, lower, upper, alpha=0.2)
            axis.set_ylabel(ylabel)
        temp_hcs = [hc for hc, values in zip(hcs, temperatures) if values.size]
        temp_values = [values.mean() for values in temperatures if values.size]
        axes[2].plot(temp_hcs, temp_values, marker="o", label=label)

    axes[1].set_yscale("symlog", linthresh=1e-8)
    axes[2].set_ylabel("DRAM temperature (C)")
    axes[2].set_xlabel("HC per aggressor")
    for axis in axes:
        axis.grid(True, alpha=0.3)
    if len(groups) <= 12:
        axes[0].legend(fontsize="small")
    return figure


def plot_hcfirst(rows, output, pyplot):
    groups = {}
    for row in rows:
        key = (row["source"], row["Temperature"], row.get("mode", ""),
               row.get("victim", ""), row.get("steps", ""),
               row["pattern"], row["aggressor_pattern"])
        groups.setdefault(key, []).append(row)

    temperatures = sorted({row["Temperature"] for row in rows})
    color_map = pyplot.get_cmap("tab10")
    temperature_colors = {
        temperature: color_map(index)
        for index, temperature in enumerate(temperatures)
    }
    figure, axes = pyplot.subplots(3, 1, figsize=(11, 10), sharex=True, constrained_layout=True)
    figure.suptitle(plot_title(rows))
    for key, group in sorted(groups.items()):
        group.sort(key=lambda row: row["repetition"])
        label = condition_label(group, include_steps=True)
        color = temperature_colors[group[0]["Temperature"]]
        repetitions = [row["repetition"] for row in group]
        thresholds = [row["hcfirst_value"] for row in group]
        axes[0].plot(repetitions, thresholds, color=color, marker=".",
                     linestyle="None", label=label)
        axes[1].plot(repetitions, [row["flips"] for row in group],
                     color=color, marker=".", linestyle="None", label=label)

    axes[0].set_ylabel("HCFirst (HC/aggressor)")
    axes[1].set_ylabel("Flipped bits")
    axes[1].set_xlabel("Repetition")
    axes[0].set_yscale("log")
    axes[0].grid(True, alpha=0.3)
    axes[1].grid(True, alpha=0.3)
    add_temperature_axis(axes[2], groups)
    from matplotlib.lines import Line2D

    temperature_handles = [
        Line2D([0], [0], color=temperature_colors[temperature], marker=".",
               linestyle="None", label=temperature)
        for temperature in temperatures
    ]
    axes[0].legend(handles=temperature_handles, title="Tested temperature")
    return figure


def plot(rows, output):
    try:
        import matplotlib.pyplot as plt
    except ImportError as error:
        raise RuntimeError("plotting requires matplotlib; install it with: pip install matplotlib") from error

    experiments = {row["experiment"] for row in rows}
    if experiments - {"ber", "hcfirst"}:
        raise ValueError(f"unsupported experiment types: {', '.join(sorted(experiments))}")
    if len(experiments) != 1:
        raise ValueError("combine only BER files with BER files or HCFirst files with HCFirst files")
    if "hcfirst" in experiments:
        figure = plot_hcfirst(rows, output, plt)
    elif len({row.get("hc_per_aggressor", "") for row in rows}) > 1:
        figure = plot_ber_sweep(rows, output, plt)
    else:
        figure = plot_ber(rows, output, plt)
    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=160)
    plt.close(figure)
    print(f"Saved plot: {output}")
    distribution_output = output.parent / f"{output.parent.name}_distribution.png"
    distribution_figure = plot_distribution(rows, plt)
    distribution_figure.savefig(distribution_output, dpi=160)
    plt.close(distribution_figure)
    print(f"Saved distribution plot: {distribution_output}")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv", nargs="+", type=Path,
                        help="summary.csv file or result directory")
    parser.add_argument("--output", type=Path,
                        help="PNG output path; default is <run-folder>.png inside the first CSV folder")
    args = parser.parse_args(argv)
    try:
        rows = load_rows(args.csv)
        result_directory = find_csv(args.csv[0]).parent
        output = args.output or (result_directory / f"{result_directory.name}.png")
        plot(rows, output)
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(2, f"{parser.prog}: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
