#!/usr/bin/env python3
"""Plot selected BER and HCFirst patterns from the results directory."""

import argparse
import csv
from pathlib import Path

import plot_results

PLOTS = (
    ("ber", "A0V1", "all_ber_a0v1_results.png"),
    ("ber", "A1V0", "all_ber_a1v0_results.png"),
    ("hcfirst", "A1V1", "all_hcfirst_a1v1_results.png"),
    ("hcfirst", "A0V0", "all_hcfirst_a0v0_results.png"),
)
BER_HC_RANGE = (300_000, 500_000)
BER_RANGE_PLOTS = (
    ("A0V1", "ber_a0v1_hc300k_500k_results.png"),
    ("A1V0", "ber_a1v0_hc300k_500k_results.png"),
)


def find_result_directories(results_directory: Path) -> list[Path]:
    """Return immediate run directories containing summary.csv."""
    return sorted(
        path for path in results_directory.iterdir()
        if path.is_dir() and (path / "summary.csv").is_file()
    )


def plot_experiment(rows: list[dict], experiment: str, output: Path) -> None:
    """Create and save one combined plot for a single experiment and pattern."""
    try:
        import matplotlib.pyplot as pyplot
    except ImportError as error:
        raise RuntimeError(
            "plotting requires matplotlib; install it with: pip install matplotlib"
        ) from error

    if experiment == "ber":
        if len({row.get("hc_per_aggressor", "") for row in rows}) > 1:
            figure = plot_results.plot_ber_sweep(rows, output, pyplot)
        else:
            figure = plot_results.plot_ber(rows, output, pyplot)
    elif experiment == "hcfirst":
        figure = plot_results.plot_hcfirst(rows, output, pyplot)
    else:
        raise ValueError(f"unsupported experiment type: {experiment}")

    output.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(output, dpi=160)
    pyplot.close(figure)
    print(f"Saved plot: {output}")


def source_run_ids(results_directory: Path) -> dict[str, str]:
    index = results_directory / "runs_index.csv"
    if not index.is_file():
        return {}
    with index.open(newline="") as stream:
        return {
            row["folder"]: row["run_id"]
            for row in csv.DictReader(stream)
            if row.get("folder") and row.get("run_id")
        }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results-directory",
        type=Path,
        default=Path(__file__).with_name("results"),
        help="directory containing the individual result directories",
    )
    args = parser.parse_args(argv)
    results_directory = args.results_directory.resolve()

    try:
        if not results_directory.is_dir():
            raise ValueError(f"results directory does not exist: {results_directory}")

        result_directories = find_result_directories(results_directory)
        if not result_directories:
            raise ValueError(f"no result directories found in {results_directory}")

        analysis_directory = results_directory / "analysis"
        analysis_directory.mkdir(exist_ok=True)
        run_ids = source_run_ids(results_directory)
        rows_by_experiment = {"ber": [], "hcfirst": []}
        directories_by_experiment = {"ber": 0, "hcfirst": 0}
        for directory in result_directories:
            rows = plot_results.load_rows([directory])
            experiments = {row["experiment"] for row in rows}
            if len(experiments) != 1 or not experiments.issubset(rows_by_experiment):
                raise ValueError(
                    f"{directory} contains unsupported or mixed experiments: "
                    + ", ".join(sorted(experiments))
                )
            experiment = next(iter(experiments))
            rows_by_experiment[experiment].extend(rows)
            directories_by_experiment[experiment] += 1

        for experiment, pattern, filename in PLOTS:
            pattern_rows = [
                row
                for row in rows_by_experiment[experiment]
                if plot_results.pattern_label(
                    row["pattern"], row["aggressor_pattern"]
                ) == pattern
            ]
            if not pattern_rows:
                raise ValueError(
                    f"no {experiment} rows found for pattern {pattern}"
                )
            output = analysis_directory / filename
            plot_experiment(
                pattern_rows,
                experiment,
                output,
            )
            source_ids = sorted({
                run_ids.get(row["source"], row["source"])
                for row in pattern_rows
            })
            output.with_suffix(".txt").write_text(
                "Source run IDs:\n" + "\n".join(source_ids) + "\n"
            )

        for pattern, filename in BER_RANGE_PLOTS:
            pattern_rows = [
                row
                for row in rows_by_experiment["ber"]
                if plot_results.pattern_label(
                    row["pattern"], row["aggressor_pattern"]
                ) == pattern
                and row.get("hc_per_aggressor", "")
                and BER_HC_RANGE[0] <= int(row["hc_per_aggressor"]) <= BER_HC_RANGE[1]
            ]
            if not pattern_rows:
                raise ValueError(
                    f"no BER rows found for pattern {pattern} in HC range "
                    f"{BER_HC_RANGE[0]}-{BER_HC_RANGE[1]}"
                )
            output = analysis_directory / filename
            plot_experiment(
                pattern_rows,
                "ber",
                output,
            )
            source_ids = sorted({
                run_ids.get(row["source"], row["source"])
                for row in pattern_rows
            })
            output.with_suffix(".txt").write_text(
                "Source run IDs:\n" + "\n".join(source_ids) + "\n"
            )

        print(
            f"BER result directories: {directories_by_experiment['ber']}"
        )
        print(
            f"HCFirst result directories: {directories_by_experiment['hcfirst']}"
        )
        return 0
    except (OSError, RuntimeError, ValueError) as error:
        parser.exit(2, f"{parser.prog}: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
